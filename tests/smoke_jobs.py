"""PRD §2.11 定时任务 HTTP 级冒烟：自带起停服务（独立库 + 真调度环，无需预先启动）。

覆盖（每轮全新临时库，可重复执行）：
① 建 notify_scan（interval 30s）作业 → 200 且 next_run_at 已换算；
② 建工作流 + workflow_run 作业（interval 30s）→ 绑定校验通过；
③ 坏调度（at 缺前导零）1001 拒收；
④ 真调度环到点自动执行：workflow_run 作业 last_run_at 出现且结果 status=ok（≤45s 轮询）；
⑤ notify_scan 作业同样被环触发（last_run_at 非空）；
⑥ run-now 手动触发不推进 next_run_at（定时节奏独立）；
⑦ daily 作业停用后调度环不再触发（last_run_at 保持为空）；
⑧ POST /jobs/tick 手动扫描入口可用（admin）；
⑨ 删除作业后 run 1004。

运行：python tests/smoke_jobs.py
输出：逐步 PASS/FAIL + RESULT 汇总；退出码 0=全过 1=有失败。
对齐：智能办公Agent 产品需求文档.md §2.11（定时任务/场景串联）；AGENTS.md §5。
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
PORT = 8217
API = f"http://127.0.0.1:{PORT}/api/v1"
ADMIN = ("admin", "admin123")

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


def start_service(log_path: Path) -> subprocess.Popen:
    """起服务：独立临时库 + SCHEDULER_ENABLED=true + tick 5s（验证真实调度环）。"""
    env = dict(os.environ)
    db_file = Path(tempfile.gettempdir()) / "office-agent-jobs-smoke.db"
    db_file.unlink(missing_ok=True)
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_file.as_posix()}",
            "RUNTIME_ENABLED": "false",
            "SCHEDULER_ENABLED": "true",
            "SCHEDULER_TICK_SECONDS": "5",
        }
    )
    launcher = (
        "import uvicorn;"
        "from office_agent_server.__main__ import create_runtime_app;"
        f"uvicorn.run(create_runtime_app(), host='127.0.0.1', port={PORT}, log_level='warning')"
    )
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(
            [sys.executable, "-c", launcher], cwd=str(REPO), env=env, stdout=log, stderr=log
        )


def wait_ready(log_path: Path) -> bool:
    """等服务就绪（最长 60s；失败打印服务日志尾部便于排障）。"""
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
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


def find_job(client: httpx.Client, headers: dict, job_id: str) -> dict:
    """从列表取作业最新态（冒烟不另开详情端点）。"""
    rows = client.get("/jobs", headers=headers).json().get("data") or []
    return next((item for item in rows if item["id"] == job_id), {})


def wait_fired(client: httpx.Client, headers: dict, job_id: str, seconds: int) -> dict:
    """轮询等调度环触发（interval 30s + tick 5s，最长 seconds）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        row = find_job(client, headers, job_id)
        if row.get("last_run_at"):
            return row
        time.sleep(2)
    return find_job(client, headers, job_id)


def main() -> int:
    """冒烟主流程（服务 → 九步断言）。"""
    log_path = Path(tempfile.gettempdir()) / "office-agent-jobs-smoke.log"
    service = start_service(log_path)
    try:
        if not wait_ready(log_path):
            check("服务启动就绪", False, "见上方服务日志")
            return 1
        with httpx.Client(base_url=API, trust_env=False, timeout=30) as client:
            admin = login(client, ADMIN)

            # ① 建 notify_scan（interval 30s）
            job_a = client.post(
                "/jobs",
                headers=admin,
                json={
                    "name": "冒烟-通知扫描",
                    "job_type": "notify_scan",
                    "schedule": {"kind": "interval", "seconds": 30},
                    "payload": {},
                },
            ).json()
            job_a_id = (job_a.get("data") or {}).get("id", "")
            check(
                "① 建 notify_scan 作业且 next_run_at 已换算",
                job_a.get("code") == 0 and bool((job_a.get("data") or {}).get("next_run_at")),
                str(job_a)[:200],
            )

            # ② 建工作流 + workflow_run 作业
            wf = client.post(
                "/workflows",
                headers=admin,
                json={
                    "name": "冒烟-简报链",
                    "steps": [{"tool": "office.schedule.view", "args": {}}],
                },
            ).json()
            wf_id = (wf.get("data") or {}).get("id", "")
            job_b = client.post(
                "/jobs",
                headers=admin,
                json={
                    "name": "冒烟-工作流执行",
                    "job_type": "workflow_run",
                    "schedule": {"kind": "interval", "seconds": 30},
                    "payload": {"workflow_id": wf_id},
                },
            ).json()
            job_b_id = (job_b.get("data") or {}).get("id", "")
            check(
                "② workflow_run 绑定真实工作流创建成功",
                wf.get("code") == 0 and job_b.get("code") == 0 and bool(job_b_id),
                str(job_b)[:200],
            )

            # ③ 坏调度拒收
            bad = client.post(
                "/jobs",
                headers=admin,
                json={
                    "name": "冒烟-坏调度",
                    "job_type": "notify_scan",
                    "schedule": {"kind": "daily", "at": "9:00"},
                },
            ).json()
            check("③ at 缺前导零 1001 拒收", bad.get("code") == 1001, str(bad)[:150])

            # ④ 真调度环到点自动执行 workflow_run（≤45s）
            fired_b = wait_fired(client, admin, job_b_id, 45)
            result_b = {}
            with contextlib.suppress(ValueError):
                result_b = json.loads(fired_b.get("last_result") or "{}")
            check(
                "④ 调度环触发 workflow_run 且 status=ok",
                bool(fired_b.get("last_run_at")) and result_b.get("status") == "ok",
                str(fired_b)[:250],
            )

            # ⑤ notify_scan 同样被环触发
            fired_a = wait_fired(client, admin, job_a_id, 45)
            check(
                "⑤ 调度环触发 notify_scan（last_run_at 非空）",
                bool(fired_a.get("last_run_at")),
                str(fired_a)[:200],
            )

            # ⑥ run-now 不推进 next_run_at
            row_b_before = find_job(client, admin, job_b_id)
            manual = client.post(f"/jobs/{job_b_id}/run", headers=admin).json()
            row_b_after = find_job(client, admin, job_b_id)
            check(
                "⑥ run-now 执行 ok 且不推进 next_run_at",
                manual.get("code") == 0
                and (manual.get("data") or {}).get("status") == "ok"
                and row_b_after.get("next_run_at") == row_b_before.get("next_run_at"),
                str(manual)[:200],
            )

            # ⑦ daily 作业停用后不被触发
            job_c = client.post(
                "/jobs",
                headers=admin,
                json={
                    "name": "冒烟-停用每日",
                    "job_type": "notify_scan",
                    "schedule": {"kind": "daily", "at": "09:00"},
                },
            ).json()
            job_c_id = (job_c.get("data") or {}).get("id", "")
            client.put(f"/jobs/{job_c_id}", headers=admin, json={"enabled": False})
            time.sleep(12)
            row_c = find_job(client, admin, job_c_id)
            check(
                "⑦ 停用作业调度环不触发（enabled=false 且未执行）",
                row_c.get("enabled") is False and not row_c.get("last_run_at"),
                str(row_c)[:200],
            )

            # ⑧ 手动 tick 入口可用
            tick = client.post("/jobs/tick", headers=admin).json()
            check(
                "⑧ POST /jobs/tick 返回 executed 计数",
                tick.get("code") == 0 and isinstance((tick.get("data") or {}).get("executed"), int),
                str(tick)[:150],
            )

            # ⑨ 删除后 run 1004
            client.delete(f"/jobs/{job_a_id}", headers=admin)
            gone = client.post(f"/jobs/{job_a_id}/run", headers=admin).json()
            check("⑨ 删除作业后 run 1004", gone.get("code") == 1004, str(gone)[:150])

            # 收尾清理（临时库每轮重建，此处只清本会话残留防 tick 连坐）
            for jid in (job_b_id, job_c_id):
                if jid:
                    client.delete(f"/jobs/{jid}", headers=admin)
            if wf_id:
                client.delete(f"/workflows/{wf_id}", headers=admin)
    finally:
        service.terminate()

    print()
    if failures:
        print(f"RESULT: {len(failures)} failed")
        return 1
    print("RESULT: 9/9 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
