"""V1.2 批次 C 工作流编排 HTTP 级冒烟：自带起停服务（独立库，无需预先启动）。

覆盖（多场景串联 + 审批闸门 + RPA 口径同链）：
① 建工作流（两读步骤）→ step_count 落库；
② run 顺序执行 ok（步骤按序回填，trace 贯穿）；
③ 含写步骤工作流 run → 落单即停 pending_approval（后续步骤不跑）；
④ 复核员批准 → approved（以申请人身份执行写步骤）；
⑤ 未知工具建工作流 → 脏编排不入库；
⑥ 删除后详情 404。

运行：python tests/smoke_v1_2_batch_c.py
输出：逐步 PASS/FAIL + RESULT 汇总；退出码 0=全过 1=有失败。
对齐：智能办公Agent 产品需求文档.md §2.11（场景串联/可视化编排）、§5.3（RPA 联动）。
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
OFFICE_PORT = 8216
OFFICE = f"http://127.0.0.1:{OFFICE_PORT}"
API = f"{OFFICE}/api/v1"
ADMIN = ("admin", "admin123")
REVIEWER = ("reviewer", "reviewer123")

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


def start_office(log_path: Path) -> subprocess.Popen:
    """起 office-agent 服务（独立库；工作流表由 lifespan 建表兜底，迁移链另行演进 dev 库）。"""
    env = dict(os.environ)
    db_file = Path(tempfile.gettempdir()) / "office-agent-workflow-smoke.db"
    db_file.unlink(missing_ok=True)
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_file.as_posix()}",
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


def login(client: httpx.Client, account: tuple[str, str]) -> dict[str, str]:
    """登录取 token（失败直接抛错：前置不满足无继续意义）。"""
    resp = client.post("/auth/login", json={"username": account[0], "password": account[1]})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{resp.status_code} {body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


def main() -> int:
    """冒烟主流程（服务 → 七步断言）。"""
    log_path = Path(tempfile.gettempdir()) / "office-agent-workflow-smoke.log"
    office = start_office(log_path)
    try:
        if not wait_ready(log_path):
            check("服务启动就绪", False, "见上方服务日志")
            return 1
        with httpx.Client(base_url=API, trust_env=False, timeout=30) as client:
            admin = login(client, ADMIN)
            reviewer = login(client, REVIEWER)

            # ① 建工作流（两读步骤）
            created = client.post(
                "/workflows",
                headers=admin,
                json={
                    "name": "日报链",
                    "description": "拉数→出报",
                    "steps": [
                        {"tool": "office.schedule.view", "args": {"view_type": "daily"}},
                        {
                            "tool": "office.report.generate",
                            "args": {"title": "日报", "metrics": {"事项": 3}},
                        },
                    ],
                },
            ).json()
            wid = (created.get("data") or {}).get("id", "")
            check(
                "① 建工作流落库（step_count=2）",
                created.get("code") == 0 and (created.get("data") or {}).get("step_count") == 2,
                str(created)[:200],
            )

            # ② run 顺序执行 ok
            run = client.post(f"/workflows/{wid}/run", headers=admin).json()
            rundata = run.get("data") or {}
            check(
                "② run 顺序执行 ok（两步按序回填）",
                run.get("code") == 0
                and rundata.get("status") == "ok"
                and [s["tool"] for s in rundata.get("steps", [])]
                == ["office.schedule.view", "office.report.generate"],
                str(rundata)[:200],
            )

            # ③ 含写步骤 → 落单即停
            created2 = client.post(
                "/workflows",
                headers=admin,
                json={
                    "name": "待办链",
                    "steps": [
                        {"tool": "office.schedule.view", "args": {}},
                        {"tool": "office.todo.create", "args": {"title": "跟进事项"}},
                    ],
                },
            ).json()
            wid2 = (created2.get("data") or {}).get("id", "")
            run2 = client.post(f"/workflows/{wid2}/run", headers=admin).json()
            run2data = run2.get("data") or {}
            approval_id = run2data.get("approval_id", "")
            check(
                "③ 写步骤落单即停（pending + 后续没跑）",
                run2data.get("status") == "pending_approval"
                and bool(approval_id)
                and len(run2data.get("steps", [])) == 1,
                str(run2data)[:200],
            )

            # ④ 复核员批准 → 以申请人身份执行
            decided = client.post(
                f"/approvals/{approval_id}/approve", headers=reviewer, json={"reason": "smoke"}
            ).json()
            check(
                "④ 复核员批准执行写步骤",
                decided.get("code") == 0
                and (decided.get("data") or {}).get("status") == "approved",
                str(decided)[:200],
            )

            # ⑤ 未知工具脏编排不入库
            bad = client.post(
                "/workflows",
                headers=admin,
                json={"name": "坏链", "steps": [{"tool": "nope.missing", "args": {}}]},
            ).json()
            check(
                "⑤ 未知工具建工作流被拒（4005/1001）",
                bad.get("code") in (4005, 1001),
                str(bad)[:150],
            )

            # ⑥ 删除后详情 404
            client.delete(f"/workflows/{wid}", headers=admin)
            gone = client.get(f"/workflows/{wid}", headers=admin).json()
            check("⑥ 删除后详情 404", gone.get("code") == 1004, str(gone)[:150])
    finally:
        office.terminate()

    print()
    if failures:
        print(f"RESULT: {len(failures)} failed")
        return 1
    print("RESULT: 7/7 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
