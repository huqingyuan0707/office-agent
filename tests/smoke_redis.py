"""ADR-0005 阶段三 HTTP 级冒烟（Redis 键值层：缓存降级 + 调度环租约锁，可重复执行）。

口径：Redis 是**加速器不是依赖**——URL 未配置或对端不可用一律降级（向量缓存退进程内 LRU、
调度环按无锁单实例语义），检索与定时任务行为不变、绝不 500。故本脚本对「Redis 不可达」
与「Redis 可达」两种场景断言同一组行为：默认跑不可达场景验证降级，起 Docker 后传 URL 再跑一遍。

覆盖（自带起停服务，独立临时库，无需预先启动）：
① 服务就绪（缓存后端不可达不影响启动）；
② kb.ask 仍 code 0 且 retrieval_mode 在允许集合（检索口径不变）；
③ 连查两次命中来源一致、分数无口径级漂移（缓存不改变排序结果）；
④ 建 interval 30s 作业 → 真调度环到点自动执行（抢不到租约锁也不停摆）；
⑤ run-now 手动触发不推进 next_run_at（调度节奏未被锁语义改动）。

用法：
    python tests/smoke_redis.py                            # 死端口：验证降级路径（默认）
    python tests/smoke_redis.py redis://127.0.0.1:6379/0   # 起 Docker 后：验证真 Redis 行为一致
对齐：docs/ADR-0005 §3/§5；AGENTS.md §3（降级绝不 500）、§5（验证命令）。
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
PORT = 8218
API = f"http://127.0.0.1:{PORT}/api/v1"
ADMIN = ("admin", "admin123")
#: 默认指向必然不可达的端口：验证「缓存后端缺失/连不上」的降级路径
REDIS_URL = sys.argv[1] if len(sys.argv) > 1 else "redis://127.0.0.1:1/0"
ALLOWED_MODES = ("milvus", "embedding", "bigram")
QUERY = "报销超过1000元需要谁审批"

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
    """起服务：独立临时库 + REDIS_URL（可达或死端口）+ 真调度环（tick 5s）。"""
    env = dict(os.environ)
    db_file = Path(tempfile.gettempdir()) / "office-agent-redis-smoke.db"
    db_file.unlink(missing_ok=True)
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_file.as_posix()}",
            "RUNTIME_ENABLED": "false",
            "SCHEDULER_ENABLED": "true",
            "SCHEDULER_TICK_SECONDS": "5",
            "REDIS_URL": REDIS_URL,
            "REDIS_TIMEOUT_SECONDS": "1",
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


def login(client: httpx.Client) -> dict[str, str]:
    """登录取 token（前置不满足直接抛错，无继续意义）。"""
    resp = client.post("/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{resp.status_code} {body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


def ask(client: httpx.Client, headers: dict[str, str]) -> dict:
    """kb.ask 一次，返回业务出参（invoke 信封在 data.result）。"""
    body = client.post(
        "/agent/tools/kb.ask/invoke", headers=headers, json={"args": {"query": QUERY, "top_k": 3}}
    ).json()
    assert body.get("code") == 0, body
    return (body.get("data") or {}).get("result") or {}


def wait_fired(client: httpx.Client, headers: dict[str, str], job_id: str, seconds: int) -> dict:
    """轮询等调度环触发（interval 30s + tick 5s，最长 seconds）。"""
    deadline = time.time() + seconds
    row: dict = {}
    while time.time() < deadline:
        rows = client.get("/jobs", headers=headers).json().get("data") or []
        row = next((item for item in rows if item["id"] == job_id), {})
        if row.get("last_run_at"):
            return row
        time.sleep(2)
    return row


def main() -> int:
    """冒烟主流程（起服务 → 五步断言 → 停服务）。"""
    print(f"INFO | REDIS_URL={REDIS_URL}（默认死端口=验证降级；起 Docker 后传真实 URL 再跑一遍）")
    log_path = Path(tempfile.gettempdir()) / "office-agent-redis-smoke.log"
    service = start_service(log_path)
    try:
        if not wait_ready(log_path):
            check("① 服务就绪（缓存后端不可达不影响启动）", False, "见上方服务日志")
            return 1
        check("① 服务就绪（缓存后端不可达不影响启动）", True)

        with httpx.Client(base_url=API, trust_env=False, timeout=30) as client:
            admin = login(client)

            first = ask(client, admin)
            mode = str(first.get("retrieval_mode") or "")
            check(
                "② kb.ask 仍 code 0 且通道在允许集合（缓存后端不参与检索裁决）",
                first.get("count", 0) >= 1 and mode in ALLOWED_MODES,
                str({k: first.get(k) for k in ("count", "retrieval_mode")})[:200],
            )

            second = ask(client, admin)
            hits_a = [(x.get("source"), x.get("score")) for x in first.get("results") or []]
            hits_b = [(x.get("source"), x.get("score")) for x in second.get("results") or []]
            check(
                "③ 连查两次命中来源与分数稳定（缓存不改变排序口径）",
                bool(hits_a)
                and [s for s, _ in hits_a] == [s for s, _ in hits_b]
                and all(abs(a[1] - b[1]) <= 0.01 for a, b in zip(hits_a, hits_b, strict=True)),
                f"first={hits_a} second={hits_b}",
            )
            print(
                f"INFO | 通道={mode}；降级原因={first.get('retrieval_fallback_reason') or '（无）'}"
            )

            job = client.post(
                "/jobs",
                headers=admin,
                json={
                    "name": "冒烟-Redis降级下定时扫描",
                    "job_type": "notify_scan",
                    "schedule": {"kind": "interval", "seconds": 30},
                    "payload": {},
                },
            ).json()
            job_id = (job.get("data") or {}).get("id", "")
            fired = wait_fired(client, admin, job_id, 45)
            check(
                "④ 真调度环到点自动执行（抢不到租约锁也不停摆）",
                bool(job_id) and bool(fired.get("last_run_at")),
                str(fired)[:250],
            )

            before = next(
                (r for r in client.get("/jobs", headers=admin).json()["data"] if r["id"] == job_id),
                {},
            )
            manual = client.post(f"/jobs/{job_id}/run", headers=admin).json()
            after = next(
                (r for r in client.get("/jobs", headers=admin).json()["data"] if r["id"] == job_id),
                {},
            )
            check(
                "⑤ run-now 可用且不推进 next_run_at（调度节奏未被锁语义改动）",
                manual.get("code") == 0 and after.get("next_run_at") == before.get("next_run_at"),
                str(manual)[:200],
            )

            print(f"INFO | 首轮触发结果={str(fired.get('last_result'))[:200]}")
            client.delete(f"/jobs/{job_id}", headers=admin)
    finally:
        service.terminate()

    print()
    if failures:
        print(f"RESULT: {len(failures)} failed → {failures}")
        return 1
    print("RESULT: 5/5 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
