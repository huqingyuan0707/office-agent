"""定时任务单测（纯函数换算 + HTTP 级 CRUD/立即执行/到点扫描）

覆盖：compute_next_run 三形态（daily/weekly 按业务时区 Asia/Shanghai 换算 UTC、
      interval 直加）；parse_schedule 非法口径一律 1001；CRUD 往返与越权 404；
      run-now 执行 notify_scan 不推进 next_run_at；workflow_run 以创建人身份执行
      （工作流已删如实 failed 不炸）；tick 到点扫描执行并推进 next_run_at。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.11。
"""

from __future__ import annotations

from datetime import datetime

from office_agent_core.settings import settings
from office_agent_server.services import jobs as jobs_mod
from office_agent_server.services.jobs import compute_next_run, parse_schedule


def _login(client, username="admin", password="admin123"):  # type: ignore[no-untyped-def]
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


def test_compute_next_run_daily_business_tz() -> None:
    """daily 按业务时区：上海 08:30 已过则顺延次日（入参/返回都是 naive UTC）。"""
    # 2027-06-02 01:00 UTC = 上海 09:00，当天 08:30 已过 → 顺延 06-03 08:30（=00:30 UTC）
    got = compute_next_run({"kind": "daily", "at": "08:30"}, datetime(2027, 6, 2, 1, 0))
    assert got == datetime(2027, 6, 3, 0, 30)
    # 2027-06-02 00:00 UTC = 上海 08:00，当天 08:30 未到 → 当天执行
    got = compute_next_run({"kind": "daily", "at": "08:30"}, datetime(2027, 6, 2, 0, 0))
    assert got == datetime(2027, 6, 2, 0, 30)


def test_compute_next_run_weekly_and_interval() -> None:
    """weekly 找下一个周 N（周一=0）；interval 直加秒。"""
    # 2027-06-02 00:00 UTC = 上海周三 08:00；day=0（周一）→ 5 天后的 09:00 上海 = 01:00 UTC
    got = compute_next_run({"kind": "weekly", "day": 0, "at": "09:00"}, datetime(2027, 6, 2, 0, 0))
    assert got == datetime(2027, 6, 7, 1, 0)
    got = compute_next_run({"kind": "interval", "seconds": 300}, datetime(2027, 6, 2, 0, 0))
    assert got == datetime(2027, 6, 2, 0, 5)


def test_parse_schedule_rejects_invalid() -> None:
    """非法调度口径：kind 未知 / at 缺前导零 / weekly 越界 / interval 过短都 1001。"""
    from office_agent_core.errors import BusinessError

    for bad in (
        {"kind": "cron", "at": "08:00"},
        {"kind": "daily", "at": "8:30"},
        {"kind": "weekly", "day": 7, "at": "09:00"},
        {"kind": "interval", "seconds": 10},
    ):
        try:
            parse_schedule(bad)
        except BusinessError as exc:
            assert exc.code == 1001, (bad, exc)
        else:
            raise AssertionError(f"应拒绝：{bad}")


def _create_job(client, headers, **overrides):  # type: ignore[no-untyped-def]
    payload = {
        "name": "每日简报扫描",
        "job_type": "notify_scan",
        "schedule": {"kind": "daily", "at": "09:00"},
        "payload": {},
    }
    payload.update(overrides)
    resp = client.post("/api/v1/jobs", headers=headers, json=payload)
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    return body["data"]


def test_job_crud_roundtrip(client) -> None:
    """创建→列表可见→停用→删除→再执行 404。"""
    headers = _login(client)
    created = _create_job(client, headers)
    assert created["next_run_at"] and created["enabled"] is True

    items = client.get("/api/v1/jobs", headers=headers).json()["data"]
    assert any(item["id"] == created["id"] for item in items)

    disabled = client.put(
        f"/api/v1/jobs/{created['id']}", headers=headers, json={"enabled": False}
    ).json()["data"]
    assert disabled["enabled"] is False

    deleted = client.delete(f"/api/v1/jobs/{created['id']}", headers=headers).json()["data"]
    assert deleted["deleted"] == created["id"]
    gone = client.post(f"/api/v1/jobs/{created['id']}/run", headers=headers).json()
    assert gone["code"] == 1004, gone


def test_job_create_validation(client) -> None:
    """类型白名单 / 调度校验 / workflow_run 必须绑定真实存在的工作流。"""
    headers = _login(client)
    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "name": "坏类型",
            "job_type": "rmpa.run",
            "schedule": {"kind": "daily", "at": "09:00"},
        },
    )
    assert resp.json()["code"] == 1001, resp.json()

    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "name": "坏调度",
            "job_type": "notify_scan",
            "schedule": {"kind": "daily", "at": "25:00"},
        },
    )
    assert resp.json()["code"] == 1001, resp.json()

    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "name": "缺绑定",
            "job_type": "workflow_run",
            "schedule": {"kind": "interval", "seconds": 60},
            "payload": {},
        },
    )
    assert resp.json()["code"] == 1001, resp.json()

    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "name": "幽灵工作流",
            "job_type": "workflow_run",
            "schedule": {"kind": "interval", "seconds": 60},
            "payload": {"workflow_id": "nope"},
        },
    )
    assert resp.json()["code"] == 1004, resp.json()


def test_run_now_notify_scan_dispatch(client, monkeypatch) -> None:
    """run-now 分发到扫描链（打桩 scan_notifications：验分支与节奏，不真建通知防连坐）。"""
    calls: list[str] = []

    async def fake_scan(_db, *, tenant, **_kw):  # type: ignore[no-untyped-def]
        calls.append(tenant)
        return {"created": 1, "skipped_existing": 0, "by_kind": {"daily_briefing": 1}}

    monkeypatch.setattr(jobs_mod, "scan_notifications", fake_scan)
    headers = _login(client)
    created = _create_job(client, headers, name="手动简报扫描")
    body = client.post(f"/api/v1/jobs/{created['id']}/run", headers=headers).json()
    assert body["code"] == 0, body
    assert body["data"]["status"] == "ok" and calls == ["demo-tenant"], body["data"]

    items = client.get("/api/v1/jobs", headers=headers).json()["data"]
    row = next(item for item in items if item["id"] == created["id"])
    assert row["next_run_at"] == created["next_run_at"], "run-now 不应推进 next_run_at"
    assert row["last_run_at"], "执行后应留最近执行时间"
    client.delete(f"/api/v1/jobs/{created['id']}", headers=headers)


def test_workflow_run_job_identity(client) -> None:
    """workflow_run 以创建人身份执行编排；工作流被删后如实 failed 不炸。"""
    headers = _login(client)
    wf = client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "定时简报链",
            "steps": [{"tool": "office.schedule.view", "args": {}}],
        },
    ).json()["data"]
    job = _create_job(
        client,
        headers,
        name="定时跑简报链",
        job_type="workflow_run",
        schedule={"kind": "interval", "seconds": 60},
        payload={"workflow_id": wf["id"]},
    )
    data = client.post(f"/api/v1/jobs/{job['id']}/run", headers=headers).json()["data"]
    assert data["status"] == "ok" and data["steps_done"] == 1, data

    client.delete(f"/api/v1/workflows/{wf['id']}", headers=headers)
    data = client.post(f"/api/v1/jobs/{job['id']}/run", headers=headers).json()["data"]
    assert data["status"] == "failed" and "不存在" in data["error"], data
    client.delete(f"/api/v1/jobs/{job['id']}", headers=headers)


def test_tick_due_jobs_executes_and_advances(client, monkeypatch) -> None:
    """tick 扫到点作业：执行、推进 next_run_at（扫描链打桩防污染；失败也推进由收口保证）。"""

    async def fake_scan(_db, *, tenant, **_kw):  # type: ignore[no-untyped-def]
        return {"created": 0, "skipped_existing": 0, "by_kind": {}}

    monkeypatch.setattr(jobs_mod, "scan_notifications", fake_scan)
    headers = _login(client)
    job = _create_job(
        client,
        headers,
        name="到点简报扫描",
        schedule={"kind": "interval", "seconds": 30},
    )
    monkeypatch.setattr(jobs_mod, "_now", lambda: datetime(2099, 1, 1))
    body = client.post("/api/v1/jobs/tick", headers=headers).json()
    assert body["code"] == 0, body
    fired = {item["job_id"]: item["status"] for item in body["data"]["jobs"]}
    assert fired.get(job["id"]) == "ok", fired

    items = client.get("/api/v1/jobs", headers=headers).json()["data"]
    row = next(item for item in items if item["id"] == job["id"])
    assert row["next_run_at"].startswith("2099"), row
    client.delete(f"/api/v1/jobs/{job['id']}", headers=headers)


async def test_scheduler_tick_respects_lease_lock(monkeypatch) -> None:
    """多实例护栏（ADR-0005 阶段三）：抢不到租约锁本轮不扫，抢到则照常扫。

    Redis 未配置/不可用时 kv.try_lock 恒 True（无锁单实例语义），调度环不被拖累——
    该分支由 packages/core/tests/test_kv.py 覆盖，此处只锁「锁语义 → 是否扫描」的接线。
    """
    from office_agent_core import kv
    from office_agent_server.services import scheduler

    ticks: list[str] = []

    async def fake_tick(_db, **_kw):  # type: ignore[no-untyped-def]
        ticks.append("tick")
        return {"jobs": []}

    monkeypatch.setattr(scheduler, "tick_due_jobs", fake_tick)
    monkeypatch.setattr(settings, "SCHEDULER_TICK_SECONDS", 30)

    seen: list[tuple[str, float]] = []

    async def deny(key: str, ttl: float) -> bool:
        seen.append((key, ttl))
        return False

    monkeypatch.setattr(kv, "try_lock", deny)
    await scheduler._tick_once()
    assert ticks == []  # 他实例持有租约 → 本轮跳过
    assert seen == [(kv.lock_key("scheduler:tick"), 60.0)]  # 租约 TTL = 2×tick

    async def allow(key: str, ttl: float) -> bool:
        return True

    monkeypatch.setattr(kv, "try_lock", allow)
    await scheduler._tick_once()
    assert ticks == ["tick"]
