"""M3 协议桥 + IM 审批通知 HTTP 级冒烟：本脚本自带假 MCP Server 与假 IM webhook 对端。

覆盖（每条都是「换协议不改业务代码」与「通知降级不阻断」红线的实测）：
① 启动装配：Settings 里 transport="mcp" 的提供方被 mcp-bridge 认领，经 tools/list 自动注册工具；
② 只读工具（readOnlyHint）免审批直执行，结果与溯源（transport=mcp + source_endpoint + fetched_at）齐备；
③ 未声明只读的工具**恒送审**：invoke 只落审批单，并触发 IM「有新单待处理」推送；
④ 复核员批准 → 以申请人身份经 MCP tools/call 执行（对端收到调用即成功消费）；
⑤ 审批超时扫描 → IM 聚合催办一条（幂等：二次扫描不重复推送）。

运行：python tests/smoke_mcp_im.py（自带起停服务与对端，无需预先启动）
输出：逐步 PASS/FAIL + RESULT 汇总；退出码 0=全过 1=有失败。
对齐：docs/ADR-0004-MCP协议桥与IM审批通知出站.md §3/§4；
      docs/office-agent仓库骨架与内核提取方案.md §7.4（M3 协议标准化）；产品需求文档 §2.2。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import httpx

REPO = Path(__file__).resolve().parents[1]
MCP_PORT = 8214
OFFICE_PORT = 8215
MCP_URL = f"http://127.0.0.1:{MCP_PORT}"
OFFICE = f"http://127.0.0.1:{OFFICE_PORT}"
API = f"{OFFICE}/api/v1"
ADMIN = ("admin", "admin123")
REVIEWER = ("reviewer", "reviewer123")
PROTOCOL_VERSION = "2025-06-18"

#: 假 MCP Server 的工具清单：一条声明只读、一条未声明（未知即从严 → 恒送审）
MCP_TOOLS = [
    {
        "name": "demo.order.query",
        "description": "查询订单（只读演示工具）",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "title": "订单号"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "demo.ticket.create",
        "description": "创建工单（写演示工具，未声明只读）",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "title": "工单类型"}},
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures: list[str] = []


def check(step: str, cond: bool, detail: str = "") -> None:
    """单步断言：PASS/FAIL 即时打印，失败记入汇总。"""
    print(
        ("PASS | " if cond else "FAIL | ") + step + (f"（{detail}）" if detail and not cond else "")
    )
    if not cond:
        failures.append(step)


class StubPeer(BaseHTTPRequestHandler):
    """假对端：同一端口既当 MCP Server（/mcp）又当 IM webhook 接收端（/im-hook）。"""

    mcp_calls: ClassVar[list[dict]] = []
    im_posts: ClassVar[list[dict]] = []

    def log_message(self, fmt: str, *args: object) -> None:
        """静音访问日志（冒烟输出只留断言结果）。"""
        _ = (fmt, args)

    def _reply(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/mcp"):
            self._handle_mcp(body)
            return
        StubPeer.im_posts.append({"path": self.path, "body": body})
        self._reply(200, {"code": 0, "msg": "ok"})

    def _handle_mcp(self, body: dict) -> None:
        """按 JSON-RPC 方法分发（initialize / tools/list / tools/call）。"""
        method = body.get("method")
        if method == "notifications/initialized":
            self._reply(202, {})
            return
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "smoke-stub", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            params = body.get("params") or {}
            StubPeer.mcp_calls.append(params)
            name = params.get("name")
            if name == "demo.order.query":
                result = {"structuredContent": {"order_id": "A1", "status": "paid"}}
            else:
                result = {"structuredContent": {"ticket_id": "T-1", "replayed": False}}
        else:
            self._reply(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
            return
        self._reply(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result})


def start_stub() -> ThreadingHTTPServer:
    """起假对端（线程内，随进程退出回收）。"""
    server = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), StubPeer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_office(log_path: Path) -> subprocess.Popen:
    """起 office-agent 服务（独立库 + MCP 提供方 + IM webhook 指向假对端）。"""
    env = dict(os.environ)
    db_file = Path(tempfile.gettempdir()) / "office-agent-mcp-smoke.db"
    db_file.unlink(missing_ok=True)
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_file.as_posix()}",
            "LINKAGE_PROVIDERS": json.dumps(
                {
                    "smoke-mcp": {
                        "base_url": MCP_URL,
                        "path": "/mcp",
                        "transport": "mcp",
                        "token": "smoke-token",
                    }
                }
            ),
            "IM_WEBHOOK_URL": f"{MCP_URL}/im-hook",
            "IM_WEBHOOK_TYPE": "generic",
            "APPROVAL_STALE_HOURS": "0",
            "RUNTIME_ENABLED": "false",
        }
    )
    launcher = (
        "import uvicorn;"
        "from office_agent_server.__main__ import create_runtime_app;"
        f"uvicorn.run(create_runtime_app(), host='127.0.0.1', port={OFFICE_PORT}, log_level='warning')"
    )
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(
            [sys.executable, "-c", launcher], cwd=str(REPO), env=env, stdout=log, stderr=log
        )


def wait_ready(log_path: Path) -> bool:
    """等服务就绪（最长 60s；失败打印服务日志尾部便于排障）。"""
    for _ in range(120):
        try:
            if httpx.get(f"{OFFICE}/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.5)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
    return False


def _calls_of(name: str) -> list[dict]:
    """假对端收到的指定工具调用（读工具在断言前已产生调用，故按名筛选）。"""
    return [call for call in StubPeer.mcp_calls if call.get("name") == name]


def login(client: httpx.Client, account: tuple[str, str]) -> dict[str, str]:
    """登录取 token（失败直接抛错：前置不满足无继续意义）。"""
    resp = client.post("/auth/login", json={"username": account[0], "password": account[1]})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{resp.status_code} {body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


def main() -> int:
    """冒烟主流程（对端 → 服务 → 五步断言）。"""
    stub = start_stub()
    log_path = Path(tempfile.gettempdir()) / "office-agent-mcp-smoke.log"
    office = start_office(log_path)
    try:
        if not wait_ready(log_path):
            check("服务启动就绪", False, "见上方服务日志")
            return 1
        with httpx.Client(base_url=API, trust_env=False, timeout=30) as client:
            admin = login(client, ADMIN)
            reviewer = login(client, REVIEWER)

            # ① 启动装配：MCP 工具经 tools/list 自动注册（只读免审 / 未声明恒送审）
            tools = {
                item["name"]: item
                for item in (client.get("/agent/tools", headers=admin).json()["data"]["items"])
            }
            read_tool = tools.get("demo.order.query") or {}
            write_tool = tools.get("demo.ticket.create") or {}
            check(
                "① MCP 工具经 tools/list 自动注册（只读免审 / 未声明恒送审）",
                read_tool.get("scope") == "mcp:read"
                and read_tool.get("requires_approval") is False
                and write_tool.get("scope") == "mcp:write"
                and write_tool.get("requires_approval") is True,
                str({"read": read_tool, "write": write_tool})[:300],
            )

            # ② 只读工具直执行 + 溯源齐备（transport=mcp 是本协议桥的实测标记）
            read_body = client.post(
                "/agent/tools/demo.order.query/invoke",
                headers=admin,
                json={"args": {"order_id": "A1"}},
            ).json()
            read_data = read_body.get("data") or {}
            provenance = read_data.get("provenance") or {}
            check(
                "② 只读工具直执行且溯源实测（transport=mcp + 端点 + 时刻）",
                read_data.get("status") == "ok"
                and (read_data.get("result") or {}).get("status") == "paid"
                and provenance.get("transport") == "mcp"
                and provenance.get("provider_id") == "smoke-mcp"
                and bool(provenance.get("fetched_at"))
                and "POST" in str(provenance.get("source_endpoint")),
                str(read_data)[:300],
            )

            # ③ 写工具恒送审：只落审批单，且 IM 收到「有新单待处理」
            write_body = client.post(
                "/agent/tools/demo.ticket.create/invoke",
                headers=admin,
                json={"args": {"kind": "after-sale"}},
            ).json()
            write_data = write_body.get("data") or {}
            approval_id = write_data.get("approval_id") or ""
            im_created = [
                post
                for post in StubPeer.im_posts
                if (post["body"] or {}).get("kind") == "approval_created"
            ]
            check(
                "③ 写工具恒送审 + IM 推送新单提醒（对端未收到该工具的 tools/call）",
                write_data.get("status") == "pending_approval"
                and bool(approval_id)
                and bool(im_created)
                and im_created[-1]["body"].get("ref_id") == approval_id
                and not _calls_of("demo.ticket.create"),
                str({"write": write_data, "im": im_created[-1:]})[:300],
            )

            # ④ 复核员批准 → 以申请人身份经 MCP 执行（对端确实收到 tools/call）
            approved = client.post(
                f"/approvals/{approval_id}/approve", headers=reviewer, json={"reason": "M3 冒烟"}
            ).json()
            executed = (approved.get("data") or {}).get("execution_result") or {}
            write_calls = _calls_of("demo.ticket.create")
            check(
                "④ 批准后经 MCP tools/call 执行（对端消费成功，幂等键/参数原样出站）",
                approved.get("code") == 0
                and executed.get("status") == "ok"
                and len(write_calls) == 1
                and write_calls[0].get("arguments") == {"kind": "after-sale"},
                str({"approved": executed, "calls": write_calls})[:300],
            )

            # ⑤ 超时扫描 → IM 聚合催办一条（幂等：二次扫描不重复推送）
            client.post(
                "/agent/tools/demo.ticket.create/invoke",
                headers=admin,
                json={"args": {"kind": "refund"}},
            )
            client.post("/notifications/scan", headers=reviewer).json()
            stale_posts = [
                post
                for post in StubPeer.im_posts
                if (post["body"] or {}).get("kind") == "approval_stale"
            ]
            client.post("/notifications/scan", headers=reviewer).json()
            stale_again = [
                post
                for post in StubPeer.im_posts
                if (post["body"] or {}).get("kind") == "approval_stale"
            ]
            check(
                "⑤ 审批超时扫描推送 IM 催办且二次扫描不刷屏（幂等）",
                len(stale_posts) == 1
                and "超时催办" in str(stale_posts[0]["body"].get("title"))
                and len(stale_again) == 1,
                str(stale_posts)[:300],
            )
    finally:
        office.terminate()
        stub.shutdown()

    print()
    if failures:
        print(f"RESULT: {len(failures)} failed / {len(failures) + 5} checks")
        return 1
    print("RESULT: 5/5 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
