"""定时任务服务（PRD §2.11 高阶自动化·定时任务的业务逻辑，纯函数不含 FastAPI 对象）。

职责：作业 CRUD（租户隔离）+ schedule 校验与 next_run_at 换算（纯函数）+
      execute_job() 按 job_type 白名单分发 + tick_due_jobs() 到点批量执行。
口径：job_type 白名单 notify_scan（通知扫描链，幂等去重绝不刷屏）/
      workflow_run（编排执行，payload 必带 workflow_id）；
      执行身份 = created_by，运行时查库取**实时**角色（建单后改角色即刻生效），
      查无此人/已冻结如实 failed——绝不借用他人身份、绝不落空角色直跑；
      schedule 的 daily/weekly 按业务时区（Settings.BUSINESS_TIMEZONE）解释，
      next_run_at 存换算后的 naive UTC——调度环只比对一个时间列；
      失败也推进 next_run_at（防坏作业热循环刷屏），执行异常一律 catch 不外抛。
链路：api/jobs（薄封装）/ services/scheduler（定时环）→ 本模块 → scheduled_jobs 表
      + notifications.scan_notifications + workflows.run_workflow（审批闸门在执行链里）。
对齐：AGENTS.md §3（分层/降级绝不 500）；智能办公Agent 产品需求文档.md §2.11。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_server.db import _now
from office_agent_server.models import ScheduledJob, User
from office_agent_server.services import workflows as workflow_service
from office_agent_server.services.notifications import scan_notifications

logger = logging.getLogger(__name__)

#: 作业类型白名单：新增类型必须同时在这里登记执行分支，禁止散落直调
JOB_TYPES = ("notify_scan", "workflow_run")

#: 最小执行间隔（秒）：interval 低于它等于变相忙轮询，创建即拒
MIN_INTERVAL_SECONDS = 30

_AT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_schedule(raw: Any) -> dict[str, Any]:
    """schedule 校验（非法一律 1001 可操作提示）。

    三种形态：``{"kind":"daily","at":"HH:MM"}``（业务时区每天）/
    ``{"kind":"weekly","day":0-6,"at":"HH:MM"}``（周一=0）/
    ``{"kind":"interval","seconds":N}``（N>=30）。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError as exc:
            raise BusinessError(ErrorCode.PARAM_INVALID, "调度配置不是合法 JSON") from exc
    if not isinstance(raw, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "调度配置必须是键值对")
    kind = str(raw.get("kind") or "")
    if kind == "interval":
        seconds = raw.get("seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            raise BusinessError(ErrorCode.PARAM_INVALID, "interval 调度缺少整数 seconds")
        if seconds < MIN_INTERVAL_SECONDS:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"执行间隔不得小于 {MIN_INTERVAL_SECONDS} 秒"
            )
        return {"kind": "interval", "seconds": seconds}
    if kind not in ("daily", "weekly"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"调度 kind 仅支持 daily/weekly/interval（收到 {kind or '空'}）",
        )
    at = str(raw.get("at") or "")
    if not _AT_RE.match(at):
        raise BusinessError(ErrorCode.PARAM_INVALID, "at 需为 HH:MM 两位补零格式（如 08:30）")
    if kind == "weekly":
        day = raw.get("day")
        if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6:
            raise BusinessError(ErrorCode.PARAM_INVALID, "weekly 的 day 需为 0-6 整数（周一=0）")
        return {"kind": "weekly", "day": day, "at": at}
    return {"kind": "daily", "at": at}


def _business_tz():  # type: ignore[no-untyped-def]
    """业务时区（缺 tzdata 降级 UTC 只告警——与 affairs.business_now 同口径）。"""
    try:
        return ZoneInfo(settings.BUSINESS_TIMEZONE)
    except Exception:
        logger.warning("业务时区 %s 不可用，调度按 UTC 解释", settings.BUSINESS_TIMEZONE)
        return UTC


def compute_next_run(schedule: dict[str, Any], now_utc: datetime) -> datetime:
    """下一次执行时点（入参/返回都是 naive UTC；daily/weekly 按业务时区换算）。"""
    kind = schedule["kind"]
    if kind == "interval":
        return now_utc + timedelta(seconds=schedule["seconds"])
    tz = _business_tz()
    local = now_utc.replace(tzinfo=UTC).astimezone(tz)
    hour, minute = (int(part) for part in schedule["at"].split(":"))
    if kind == "daily":
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
    else:
        days_ahead = (schedule["day"] - local.weekday()) % 7
        candidate = (local + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= local:
            candidate += timedelta(days=7)
    return candidate.astimezone(UTC).replace(tzinfo=None)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def to_dict(row: ScheduledJob) -> dict[str, Any]:
    """作业出参（schedule/payload 解析回对象；last_result 给原文不重塑）。"""
    try:
        schedule = parse_schedule(row.schedule)
    except BusinessError:
        schedule = {}
    return {
        "id": row.id,
        "name": row.name,
        "job_type": row.job_type,
        "schedule": schedule,
        "payload": _loads_or_empty(row.payload),
        "enabled": bool(row.enabled),
        "next_run_at": row.next_run_at.isoformat(sep=" ", timespec="seconds")
        if row.next_run_at
        else "",
        "last_run_at": row.last_run_at.isoformat(sep=" ", timespec="seconds")
        if row.last_run_at
        else "",
        "last_result": row.last_result,
        "created_by": row.created_by,
    }


def _loads_or_empty(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def _get_or_raise(db: AsyncSession, tenant: str, job_id: str) -> ScheduledJob:
    """按租户取作业；取不到 404（越权与不存在同口径）。"""
    row = (
        await db.execute(
            select(ScheduledJob).where(ScheduledJob.tenant == tenant, ScheduledJob.id == job_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "定时任务不存在或无权访问", 404)
    return row


async def list_jobs(db: AsyncSession, *, tenant: str) -> list[dict[str, Any]]:
    """租户内作业列表（按下次执行升序，停用的排最后）。"""
    rows = (
        (
            await db.execute(
                select(ScheduledJob)
                .where(ScheduledJob.tenant == tenant)
                .order_by(ScheduledJob.enabled.desc(), ScheduledJob.next_run_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [to_dict(row) for row in rows]


async def create_job(
    db: AsyncSession,
    *,
    tenant: str,
    name: str,
    job_type: str,
    schedule: Any,
    payload: Any,
    created_by: str,
) -> dict[str, Any]:
    """新建作业（类型白名单 + schedule 校验 + workflow_run 绑定的工作流必须真实存在）。"""
    text = str(name or "").strip()
    if not text:
        raise BusinessError(ErrorCode.PARAM_INVALID, "任务名称不能为空")
    if job_type not in JOB_TYPES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"任务类型仅支持 {'/'.join(JOB_TYPES)}（收到 {job_type or '空'}）",
        )
    parsed = parse_schedule(schedule)
    body = payload if isinstance(payload, dict) else _loads_or_empty(str(payload or "{}"))
    if job_type == "workflow_run":
        wf_id = str(body.get("workflow_id") or "").strip()
        if not wf_id:
            raise BusinessError(ErrorCode.PARAM_INVALID, "workflow_run 必须指定 workflow_id")
        await workflow_service.get_workflow(db, tenant=tenant, workflow_id=wf_id)
    row = ScheduledJob(
        tenant=tenant,
        name=text[:80],
        job_type=job_type,
        schedule=_dump(parsed),
        payload=_dump(body),
        enabled=1,
        next_run_at=compute_next_run(parsed, _now()),
        created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return to_dict(row)


async def update_job(
    db: AsyncSession,
    *,
    tenant: str,
    job_id: str,
    name: str | None,
    schedule: Any,
    payload: Any,
    enabled: bool | None,
) -> dict[str, Any]:
    """更新作业（schedule 改动或重新启用都从当下重算 next_run_at——停用期间的欠账不补跑）。"""
    row = await _get_or_raise(db, tenant, job_id)
    if name is None and schedule is None and payload is None and enabled is None:
        raise BusinessError(ErrorCode.PARAM_INVALID, "没有要更新的内容")
    if name is not None:
        text = str(name).strip()
        if not text:
            raise BusinessError(ErrorCode.PARAM_INVALID, "任务名称不能为空")
        row.name = text[:80]
    if schedule is not None:
        row.schedule = _dump(parse_schedule(schedule))
    if payload is not None:
        row.payload = _dump(payload if isinstance(payload, dict) else {})
    if enabled is not None:
        row.enabled = 1 if enabled else 0
    if schedule is not None or enabled is True:
        row.next_run_at = compute_next_run(parse_schedule(row.schedule), _now())
    row.updated_at = _now()
    await db.commit()
    await db.refresh(row)
    return to_dict(row)


async def delete_job(db: AsyncSession, *, tenant: str, job_id: str) -> dict[str, str]:
    """删除作业（只删定义；历史执行结果随作业消失，审计留痕在 tool_calls 不断链）。"""
    row = await _get_or_raise(db, tenant, job_id)
    await db.delete(row)
    await db.commit()
    return {"deleted": job_id}


async def _run_workflow_job(db: AsyncSession, row: ScheduledJob) -> dict[str, Any]:
    """workflow_run 分支：以 created_by 的实时身份执行编排（查无/冻结如实 failed）。"""
    wf_id = str(_loads_or_empty(row.payload).get("workflow_id") or "")
    user = (
        await db.execute(
            select(User).where(User.tenant == row.tenant, User.username == row.created_by)
        )
    ).scalar_one_or_none()
    if user is None:
        return {"status": "failed", "error": f"创建人 {row.created_by} 不存在，无法以其身份执行"}
    if user.status != "active":
        return {"status": "failed", "error": f"创建人 {row.created_by} 已冻结，不代为执行"}
    roles = [item for item in (user.roles or "").split(",") if item]
    try:
        outcome = await workflow_service.run_workflow(
            db,
            tenant=row.tenant,
            username=row.created_by,
            roles=roles,
            workflow_id=wf_id,
        )
    except BusinessError as exc:
        return {"status": "failed", "error": exc.msg, "code": exc.code}
    return {
        "status": outcome["status"],
        "workflow_id": wf_id,
        "steps_done": len(outcome.get("steps", [])),
        "approval_id": outcome.get("approval_id", ""),
        "error": outcome.get("error", ""),
    }


async def execute_job(db: AsyncSession, row: ScheduledJob, *, trace_id: str = "") -> dict[str, Any]:
    """执行一个作业（白名单分发；任何异常都收口成 failed 结果，绝不外抛炸调度环）。"""
    try:
        if row.job_type == "notify_scan":
            summary = await scan_notifications(db, tenant=row.tenant)
            return {
                "status": "ok",
                "created": summary["created"],
                "skipped_existing": summary["skipped_existing"],
                "by_kind": summary["by_kind"],
            }
        if row.job_type == "workflow_run":
            return await _run_workflow_job(db, row)
        return {"status": "failed", "error": f"未知任务类型：{row.job_type}"}
    except Exception as exc:
        logger.warning("job %s execute failed: %s", row.id, str(exc)[:200], exc_info=True)
        return {"status": "failed", "error": str(exc)[:200]}


async def run_job_now(db: AsyncSession, *, tenant: str, job_id: str) -> dict[str, Any]:
    """立即执行（手动触发口径：只更新 last_*，**不推进** next_run_at，定时节奏不受影响）。"""
    row = await _get_or_raise(db, tenant, job_id)
    result = await execute_job(db, row)
    row.last_run_at = _now()
    row.last_result = _dump(result)[:2000]
    await db.commit()
    return {"job_id": row.id, "name": row.name, **result}


async def tick_due_jobs(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """执行全部到点的启用作业（每轮到点即推进，失败也推进防热循环；单作业故障不连坐）。"""
    due = (
        (
            await db.execute(
                select(ScheduledJob)
                .where(
                    ScheduledJob.enabled == 1,
                    ScheduledJob.next_run_at.is_not(None),
                    ScheduledJob.next_run_at <= (now or _now()),
                )
                .order_by(ScheduledJob.next_run_at.asc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    fired: list[dict[str, Any]] = []
    for row in due:
        result = await execute_job(db, row)
        fired.append({"job_id": row.id, "name": row.name, "status": result["status"]})
        row.last_run_at = _now()
        row.last_result = _dump(result)[:2000]
        row.next_run_at = compute_next_run(parse_schedule(row.schedule), _now())
        await db.commit()
    if fired:
        logger.info("scheduler tick: fired=%d", len(fired))
    return {"executed": len(fired), "jobs": fired}
