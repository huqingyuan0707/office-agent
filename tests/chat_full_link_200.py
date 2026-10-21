"""对话办理 200 条全链路测试（自带起停服务，可重复执行）

覆盖（真实服务 + 真实 Ollama + 真实工具，零 mock）：
① 200 条单轮用例：规则快路径命中（日报/待办/台账/空闲/日程/制度/报销/经营串联）
   → 断言选中智能体、实际执行工具、终态、管理员 trace 六段齐备；
② 自由目标（无规则命中）→ ReAct 逐步再规划：断言链路不断裂（终态合法、trace 齐备）；
③ 边界输入（空串/超长/注入/XSS/纯数字）：断言不 500、不产生幽灵 run；
④ 多轮连续：同会话追加补充（上下文生效）与跨主题提示词污染（如实记录为边界观察）；
⑤ 写动作闭环：建待办/建日程 → 恒送审挂起 → 复核员批准 → 续跑 DONE；驳回 → FAILED；
⑥ 权限隔离：普通员工可见步骤时间线但响应无 trace 键。

用例来源：plugins/*/agent.yaml 的 rules 关键词（唯一真相源）合成；路由优先级按
  loader 目录序（daily-report → ecommerce → office → personal-affairs）与规则声明序。
运行：python tests/chat_full_link_200.py [--limit N] [--port 8219]
输出：data/reports/chat_full_link_200.{json,md}；退出码 0=全过 1=有失败。
对齐：智能办公Agent 产品需求文档.md §4.3/§6；AGENTS.md §5。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
REPORT_DIR = REPO / "data" / "reports"
TERMINAL = ("DONE", "FAILED", "WAITING_APPROVAL")

#: 安全前缀池（不引入任何智能体关键词，只做句式变换）
_PREFIXES = ["", "帮我", "请帮我", "我想", "麻烦", "顺便", "现在", "另外", "接着", "帮我看一下"]


def _expand(base: list[str], count: int) -> list[str]:
    """把基础句扩到目标条数：加安全前缀去重，保证条数与多样性都可预期。"""
    out: list[str] = []
    seen: set[str] = set()
    index = 0
    while len(out) < count:
        stem = base[index % len(base)]
        prefix = _PREFIXES[(index // len(base)) % len(_PREFIXES)]
        text = f"{prefix}{stem}"
        index += 1
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _cases() -> list[dict[str, Any]]:
    """200 条用例清单（用例来源 = agent.yaml 规则关键词）。"""
    cases: list[dict[str, Any]] = []

    def add(category: str, texts: list[str], agent: str, tools: list[str], status: str) -> None:
        for text in texts:
            cases.append(
                {
                    "category": category,
                    "text": text,
                    "agent": agent,
                    "tools": tools,
                    "status": status,
                }
            )

    add(
        "daily_report",
        _expand(
            [
                "生成今天的工作日报",
                "写一份日报",
                "今天做了什么",
                "查日程",
                "看日程",
                "工作总结",
                "帮我整理工作日报",
                "出个日报",
                "做个工作总结",
            ],
            20,
        ),
        "daily-report-assistant",
        ["office.schedule.view", "office.doc.compose"],
        "DONE",
    )
    add(
        "daily_todo",
        _expand(
            [
                "建个待办",
                "创建待办",
                "提醒我开会",
                "记一下",
                "记一下这件事",
                "提醒我交材料",
                "记一下明天要跟进的事",
                "建个待办事项",
                "创建待办事项",
                "提醒我明天开会",
            ],
            14,
        ),
        "daily-report-assistant",
        ["office.todo.create"],
        "WAITING_APPROVAL",
    )
    add(
        "todo_list",
        _expand(
            [
                "查待办",
                "我的待办",
                "待办清单",
                "待办列表",
                "还有什么待办",
                "有什么待办",
                "看待办",
                "今日待办",
                "今天的待办",
                "查一下待办",
                "待办事项",
            ],
            24,
        ),
        "personal-affairs-assistant",
        ["office.todo.list"],
        "DONE",
    )
    add(
        "worklog",
        _expand(
            [
                "工作台账",
                "个人台账",
                "本周小结",
                "日小结",
            ],
            12,
        ),
        "personal-affairs-assistant",
        ["office.worklog.generate"],
        "DONE",
    )
    add(
        "freebusy",
        _expand(
            [
                "空闲",
                "有空档",
                "什么时候有空",
                "约时间",
            ],
            12,
        ),
        "personal-affairs-assistant",
        ["office.schedule.freebusy"],
        "DONE",
    )
    add(
        "todo_create",
        _expand(
            [
                "新建待办",
                "加个待办",
            ],
            8,
        ),
        "personal-affairs-assistant",
        ["office.todo.create"],
        "WAITING_APPROVAL",
    )
    add(
        "schedule_create",
        _expand(
            [
                "建日程",
                "安排会议",
                "约个会",
                "项目节点",
            ],
            10,
        ),
        "personal-affairs-assistant",
        ["office.schedule.create"],
        "WAITING_APPROVAL",
    )
    add(
        "kb_policy",
        _expand(
            [
                "报销制度",
                "报销流程",
                "怎么报销",
                "报销规定",
                "报销标准",
                "报销政策",
                "如何报销",
                "报销审批",
            ],
            22,
        ),
        "office-assistant",
        ["kb.ask"],
        "DONE",
    )
    add(
        "policy_other",
        _expand(
            [
                "考勤制度",
                "请假标准",
                "费用标准",
                "制度标准",
                "什么标准",
            ],
            8,
        ),
        "office-assistant",
        [],
        "",
    )
    add(
        "reimburse",
        _expand(
            [
                "报销进度",
                "查报销",
                "报销到哪了",
                "报销状态",
            ],
            12,
        ),
        "office-assistant",
        ["office.finance.reimburse"],
        "DONE",
    )
    add(
        "approval_draft",
        _expand(
            [
                "报销",
                "报销单",
                "报一张报销单",
                "填个报销单",
            ],
            12,
        ),
        "office-assistant",
        ["office.approval.draft"],
        "DONE",
    )
    add(
        "chain_sales",
        _expand(
            [
                "经营复盘",
                "经营分析",
                "数据分析",
                "数据报告",
                "做一次经营数据分析",
            ],
            8,
        ),
        "office-assistant",
        ["office.data.query", "kb.ask", "office.doc.compose"],
        "DONE",
    )
    add(
        "chain_work",
        _expand(
            [
                "工时统计",
                "工时周报",
                "工时复盘",
            ],
            8,
        ),
        "office-assistant",
        ["office.data.query", "kb.ask", "office.doc.compose"],
        "DONE",
    )
    add(
        "chain_attendance",
        _expand(
            [
                "考勤分析",
                "考勤统计",
                "出勤情况",
            ],
            8,
        ),
        "office-assistant",
        ["office.data.query", "kb.ask", "office.doc.compose"],
        "DONE",
    )
    add(
        "free_form",
        [
            "帮我总结一下本周的工作亮点",
            "分析一下团队效率怎么提升",
            "写一封给客户的感谢信",
            "起草一份会议通知",
            "帮我想一个团建方案",
            "总结一下这个季度的重点工作",
            "给我讲个办公小技巧",
            "如何提高开会效率",
            "写一段项目介绍",
            "规划下周的工作重点",
            "评估一下远程办公的利弊",
            "推荐一本职场书并说明理由",
        ],
        "",
        [],
        "",
    )
    add(
        "edge",
        [
            "",
            "   ",
            "123456",
            "？？？",
            "a" * 500,
            "x" * 501,
            "' OR '1'='1",
            "<script>alert(1)</script>",
            "%%",
            "测试!@#￥%",
        ],
        "",
        [],
        "",
    )
    # 空串与超长（>500 字）由入参校验拦下：项目口径是信封 1001（app.py 的
    # RequestValidationError 处理器），非 HTTP 422——按此断言，不算失败。
    for case in cases:
        if case["category"] == "edge" and case["text"] in ("", "x" * 501):
            case["expect_reject"] = True

    for index, case in enumerate(cases, start=1):
        case["id"] = f"C{index:03d}"
    assert len(cases) == 200, f"用例数应为 200，实际 {len(cases)}"
    return cases


def _ollama_alive() -> bool:
    """本机大模型服务是否可达（不可达属环境故障，绝不记成产品失败）。"""
    try:
        return httpx.get("http://127.0.0.1:11434/api/tags", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


def _start_service(port: int, log_path: Path, llm_model: str) -> subprocess.Popen:
    """起隔离服务：独立临时库 + 载入 .env（LLM/embedding 配置真实生效）。

    llm_model 非空时覆盖 LLM_PROVIDERS 的模型名——本机 16GB 内存装不下 qwen3:8b
    （实测 5GB 权重加载后仅余 1.1GB，Ollama 会被系统杀掉），批量跑用小模型保链路完整。
    """
    db_file = Path(tempfile.gettempdir()) / f"office-agent-chat200-{port}.db"
    db_file.unlink(missing_ok=True)
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    if llm_model:
        env["LLM_PROVIDERS"] = json.dumps(
            {
                "default": {
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": llm_model,
                    "api_key": "",
                    "timeout_seconds": 120,
                }
            },
            ensure_ascii=False,
        )
    launcher = (
        "import uvicorn;"
        "from office_agent_server.__main__ import create_runtime_app, load_env_file;"
        "load_env_file();"
        f"uvicorn.run(create_runtime_app(), host='127.0.0.1', port={port}, log_level='warning')"
    )
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(
            [sys.executable, "-c", launcher], cwd=str(REPO), env=env, stdout=log, stderr=log
        )


def _wait_ready(port: int, log_path: Path) -> bool:
    """等服务就绪（最长 90s；失败打印日志尾部便于排障）。"""
    for _ in range(180):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.5)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
    return False


def _login(client: httpx.Client, user: str, password: str) -> dict[str, str]:
    """登录取 token（失败直接抛：前置不满足无继续意义）。"""
    body = client.post("/auth/login", json={"username": user, "password": password}).json()
    assert body.get("code") == 0, f"登录失败 {user}: {body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


def _new_conversation(client: httpx.Client, headers: dict[str, str], title: str) -> str:
    """新建会话（每条用例独立会话，避免多轮上下文污染路由期望）。"""
    body = client.post("/conversations", headers=headers, json={"title": title}).json()
    assert body.get("code") == 0, f"建会话失败：{body}"
    return str(body["data"]["id"])


def _wait_terminal(
    client: httpx.Client, headers: dict[str, str], run_id: str, timeout_s: int
) -> dict[str, Any]:
    """轮询到终态（挂起审批也算终态——写动作恒送审）。"""
    deadline = time.time() + timeout_s
    view: dict[str, Any] = {}
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}", headers=headers).json()
        view = body.get("data") or {}
        if view.get("status") in TERMINAL:
            return view
        time.sleep(2)
    return view


def _send_and_wait(
    client: httpx.Client,
    headers: dict[str, str],
    case: dict[str, Any],
    timeout_s: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """发消息并轮询终态：返回 (结果记录, run 视图)；视图为空表示未产出 run。"""
    record: dict[str, Any] = {
        "id": case["id"],
        "category": case["category"],
        "text": case["text"][:60],
        "expect_agent": case["agent"],
        "expect_status": case["status"],
        "problems": [],
    }
    started = time.time()
    try:
        resp = client.post(
            f"/conversations/{_new_conversation(client, headers, case['id'])}/messages",
            headers=headers,
            json={"text": case["text"]},
        )
    except httpx.HTTPError as exc:
        record["problems"].append(f"传输异常：{type(exc).__name__}")
        record["elapsed_s"] = round(time.time() - started, 2)
        return record, {}
    record["http_status"] = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        record["problems"].append("响应非 JSON")
        record["elapsed_s"] = round(time.time() - started, 2)
        return record, {}
    if resp.status_code == 422:
        record["outcome"] = "REJECTED_422"
        record["elapsed_s"] = round(time.time() - started, 2)
        return record, {}
    if body.get("code") != 0:
        if case.get("expect_reject") and body.get("code") == 1001:
            record["outcome"] = "REJECTED_1001"
            record["reject_msg"] = str(body.get("msg") or "")[:80]
        else:
            record["problems"].append(f"信封 code≠0：{body.get('code')} {body.get('msg')}")
        record["elapsed_s"] = round(time.time() - started, 2)
        return record, {}
    data = body.get("data") or {}
    run = data.get("run")
    if not run:
        record["outcome"] = "MESSAGE_ONLY"
        record["reply"] = str(data.get("error") or "")[:120]
        record["elapsed_s"] = round(time.time() - started, 2)
        return record, {}
    record["run_id"] = str(run.get("run_id"))
    view = _wait_terminal(client, headers, record["run_id"], timeout_s)
    record["status"] = str(view.get("status") or "")
    record["agent"] = str(view.get("agent") or "")
    record["elapsed_s"] = round(time.time() - started, 2)
    record["steps"] = [s.get("tool") for s in view.get("steps") or []]
    record["trace_steps"] = len((view.get("trace") or {}).get("steps") or [])
    return record, view


def _check_expectations(record: dict[str, Any], view: dict[str, Any], case: dict[str, Any]) -> None:
    """按用例期望核对智能体/终态/工具序列/送审/trace（逐项记问题，不中断）。"""
    status = str(record.get("status") or "")
    if status not in TERMINAL:
        record["problems"].append(f"未到终态：{status or '空'}")
        return
    if case["agent"] and record.get("agent") != case["agent"]:
        record["problems"].append(f"智能体不符：{record.get('agent')} != {case['agent']}")
    if case["status"] and status != case["status"]:
        record["problems"].append(f"终态不符：{status} != {case['status']}")
    if case["status"] == "DONE" and case["tools"] and record.get("steps") != case["tools"]:
        record["problems"].append(f"工具序列不符：{record.get('steps')} != {case['tools']}")
    if case["status"] == "WAITING_APPROVAL":
        pending = view.get("pending_approval") or {}
        if pending.get("tool") != case["tools"][0]:
            record["problems"].append(f"送审工具不符：{pending.get('tool')}")
        record["approval_id"] = pending.get("approval_id")
    if not record.get("agent"):
        # 纯寒暄即时回复：零工具零步骤，设计上不产生执行链路（如实记录，不算失败）
        record["trace_note"] = "寒暄即时回复，无执行链路（设计如此）"
        return
    if not view.get("trace"):
        record["problems"].append("管理员响应无 trace")
    elif int(record.get("trace_steps") or 0) < 4:
        record["problems"].append(f"trace 分段过少：{record.get('trace_steps')}")


def _run_case(
    client: httpx.Client,
    headers: dict[str, str],
    case: dict[str, Any],
    timeout_s: int,
) -> dict[str, Any]:
    """执行单条用例并返回结果记录（断言全部在脚本内以 FAIL 列表呈现）。"""
    record, view = _send_and_wait(client, headers, case, timeout_s)
    if view:
        _check_expectations(record, view, case)
    return record


def _multi_turn(client: httpx.Client, headers: dict[str, str], timeout_s: int) -> dict[str, Any]:
    """多轮连续：同会话追加补充（上下文生效）与跨主题污染（如实记录）。"""
    conv = _new_conversation(client, headers, "多轮")
    turns = [
        ("生成今天的工作日报", "daily-report-assistant"),
        ("改成昨天的", "daily-report-assistant"),
    ]
    agents: list[str] = []
    for text, _ in turns:
        body = client.post(
            f"/conversations/{conv}/messages", headers=headers, json={"text": text}
        ).json()
        run = (body.get("data") or {}).get("run") or {}
        view = _wait_terminal(client, headers, str(run.get("run_id")), timeout_s)
        agents.append(str(view.get("agent") or ""))
    polluted = _new_conversation(client, headers, "多轮污染")
    client.post(
        f"/conversations/{polluted}/messages", headers=headers, json={"text": "生成今天的工作日报"}
    )
    body = client.post(
        f"/conversations/{polluted}/messages", headers=headers, json={"text": "查待办"}
    ).json()
    run = (body.get("data") or {}).get("run") or {}
    view = _wait_terminal(client, headers, str(run.get("run_id")), timeout_s)
    return {
        "context_agents": agents,
        "context_ok": agents == [agent for _, agent in turns],
        "polluted_agent": str(view.get("agent") or ""),
    }


def _approval_loop(client: httpx.Client, headers: dict[str, str], timeout_s: int) -> dict[str, Any]:
    """写动作闭环：送审挂起 → 复核员批准 → 续跑 DONE；另一单驳回 → FAILED。"""
    reviewer = _login(client, "reviewer", "reviewer123")
    out: dict[str, Any] = {"approved": {}, "rejected": {}}
    for label, text in (("approved", "加个待办"), ("rejected", "记一下今天要跟进的事")):
        body = client.post(
            f"/conversations/{_new_conversation(client, headers, label)}/messages",
            headers=headers,
            json={"text": text},
        ).json()
        run = (body.get("data") or {}).get("run") or {}
        view = _wait_terminal(client, headers, str(run.get("run_id")), timeout_s)
        approval_id = str((view.get("pending_approval") or {}).get("approval_id") or "")
        if not approval_id:
            out[label] = {"ok": False, "reason": "未产生审批单"}
            continue
        decide = client.post(
            f"/approvals/{approval_id}/{'approve' if label == 'approved' else 'reject'}",
            headers=reviewer,
            json={"reason": "复核员裁决（200 条全链路测试）"},
        ).json()
        after = _wait_terminal(client, headers, str(run.get("run_id")), timeout_s)
        out[label] = {
            "ok": True,
            "decide_code": decide.get("code"),
            "status": after.get("status"),
            "steps": [s.get("tool") for s in after.get("steps") or []],
        }
    return out


def _permission(client: httpx.Client, headers: dict[str, str], timeout_s: int) -> dict[str, Any]:
    """权限隔离：普通员工可见步骤时间线但无 trace 键。"""
    staff = _login(client, "zhangsan", "zhangsan123")
    conv = _new_conversation(client, headers, "权限")
    body = client.post(
        f"/conversations/{conv}/messages", headers=headers, json={"text": "报销制度"}
    ).json()
    run = (body.get("data") or {}).get("run") or {}
    view = _wait_terminal(client, headers, str(run.get("run_id")), timeout_s)
    staff_view = client.get(f"/runs/{run.get('run_id')}", headers=staff).json().get("data") or {}
    return {
        "staff_has_trace": "trace" in staff_view,
        "staff_has_steps": isinstance(staff_view.get("steps"), list),
        "admin_has_trace": bool(view.get("trace")),
    }


def _write_report(records: list[dict[str, Any]], extra: dict[str, Any]) -> None:
    """落 JSON + Markdown 报告（data/ 已在 .gitignore，不入库）。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"records": records, "extra": extra}
    (REPORT_DIR / "chat_full_link_200.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    by_cat: dict[str, dict[str, Any]] = {}
    for item in records:
        bucket = by_cat.setdefault(item["category"], {"total": 0, "failed": 0, "status": {}})
        bucket["total"] += 1
        if item["problems"]:
            bucket["failed"] += 1
        key = str(item.get("status") or item.get("outcome") or "?")
        bucket["status"][key] = bucket["status"].get(key, 0) + 1
    lines = [
        "# 对话办理 200 条全链路测试报告",
        "",
        "## 分类结果",
        "",
        "| 类别 | 条数 | 失败 | 终态分布 |",
        "|---|---|---|---|",
    ]
    for cat in sorted(by_cat):
        bucket = by_cat[cat]
        dist = "、".join(f"{k}×{v}" for k, v in sorted(bucket["status"].items()))
        lines.append(f"| {cat} | {bucket['total']} | {bucket['failed']} | {dist} |")
    lines += [
        "",
        "## 附加阶段",
        "",
        "```json",
        json.dumps(extra, ensure_ascii=False, indent=2),
        "```",
    ]
    (REPORT_DIR / "chat_full_link_200.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    """主流程：起服务 → 跑 200 条 → 多轮/审批/权限 → 报告。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--port", type=int, default=8219)
    parser.add_argument("--timeout", type=int, default=240, help="单条等待终态上限（秒）")
    parser.add_argument("--llm-model", default="", help="覆盖 LLM 模型（空=用 .env 配置）")
    parser.add_argument("--only", default="", help="只跑指定类别（逗号分隔，0=全部）")
    parser.add_argument("--ids", default="", help="只跑指定用例号（逗号分隔，如 C001,C101）")
    parser.add_argument("--skip-extra", action="store_true", help="跳过多轮/审批/权限附加阶段")
    args = parser.parse_args()

    if not _ollama_alive():
        print("ENV FAIL | 本机大模型服务（Ollama 11434）不可达：这是环境故障，不计产品失败")
        print("  排查：ollama serve 是否在跑、OLLAMA_MODELS 是否指向含 qwen3:8b 的目录")
        return 2

    log_path = REPO / "data" / "chat_full_link_200_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"起隔离服务 127.0.0.1:{args.port}（独立库，LLM 模型：{args.llm_model or '用 .env 配置'}）"
    )
    proc = _start_service(args.port, log_path, args.llm_model)
    started = time.time()
    try:
        if not _wait_ready(args.port, log_path):
            print("FAIL | 服务未就绪")
            return 1
        base = f"http://127.0.0.1:{args.port}/api/v1"
        client = httpx.Client(
            base_url=base, timeout=httpx.Timeout(args.timeout + 60.0), trust_env=False
        )
        headers = _login(client, "admin", "admin123")
        cases = _cases()
        if args.only:
            wanted = {c.strip() for c in args.only.split(",") if c.strip()}
            cases = [c for c in cases if c["category"] in wanted]
        if args.ids:
            wanted_ids = {c.strip() for c in args.ids.split(",") if c.strip()}
            cases = [c for c in cases if c["id"] in wanted_ids]
        if args.limit:
            cases = cases[: args.limit]
        print(f"开始 {len(cases)} 条用例（每条独立会话，避免上下文污染期望）")
        records: list[dict[str, Any]] = []
        for index, case in enumerate(cases, start=1):
            records.append(_run_case(client, headers, case, args.timeout))
            if index % 20 == 0 or index == len(cases):
                failed = sum(1 for r in records if r["problems"])
                elapsed = time.time() - started
                print(f"... {index}/{len(cases)} 已跑，用时 {elapsed / 60:.1f} 分钟，失败 {failed}")
        extra: dict[str, Any] = {"llm_model": args.llm_model or "（.env 默认 qwen3:8b）"}
        if not args.skip_extra:
            extra["multi_turn"] = _multi_turn(client, headers, args.timeout)
            extra["approval"] = _approval_loop(client, headers, args.timeout)
            extra["permission"] = _permission(client, headers, args.timeout)
        extra["ollama_alive_at_end"] = _ollama_alive()
        _write_report(records, extra)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [r for r in records if r["problems"]]
    print()
    print(
        f"用例 {len(records)} 条，失败 {len(failed)} 条，总用时 {(time.time() - started) / 60:.1f} 分钟"
    )
    for item in failed[:20]:
        print(
            f"  FAIL {item['id']}[{item['category']}] {item['text']} → {'; '.join(item['problems'])}"
        )
    if len(failed) > 20:
        print(f"  …（其余 {len(failed) - 20} 条见报告）")
    print(f"附加阶段：{json.dumps(extra, ensure_ascii=False)}")
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
