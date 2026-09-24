"""站内通知扫描服务（主动消息推送的业务逻辑，纯函数不含 FastAPI 对象）。

职责：
- scan_notifications()：扫描三类信号并生成站内通知（去重 + 上限保护）——
  ① approval_stale：pending 审批单超过 Settings.APPROVAL_STALE_HOURS 未处理 → 提醒提交人；
  ② task_failed：异步任务执行失败 → 提醒任务提交人；
  ③ daily_briefing：按活跃用户生成当日简报（待复核审批数 / 失败任务数），ref_id=日期；
- mark_read()：本人已读回执（幂等，重复已读直接返回）。

去重红线：(tenant, username, kind, ref_id) 唯一——同一事项同一人只提醒一次，
扫描可反复触发（外部定时器/cron 调 scan 端点）绝不刷屏。
对齐：AGENTS.md §3（分层红线：业务进 services/）；智能办公Agent 产品需求文档.md §5.1
（V1.0 主动消息推送）、§2.2（审批超时预警/任务到期提醒/定期简报）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_server.db import _now
from office_agent_server.models import Approval, Notification, Task, User
from office_agent_server.services import im_notifier

logger = logging.getLogger(__name__)

KIND_APPROVAL_STALE = "approval_stale"
KIND_TASK_FAILED = "task_failed"
KIND_DAILY_BRIEFING = "daily_briefing"

#: 通知类型中文名（展示层唯一出处）
KIND_LABELS: dict[str, str] = {
    KIND_APPROVAL_STALE: "审批超时提醒",
    KIND_TASK_FAILED: "任务失败提醒",
    KIND_DAILY_BRIEFING: "今日简报",
}


def _stale_cutoff(stale_hours: float | None) -> datetime:
    """超时判定时点（stale_hours 显式传入可测试；缺省取 Settings）。"""
    hours = float(stale_hours if stale_hours is not None else settings.APPROVAL_STALE_HOURS)
    return _now() - timedelta(hours=hours)


async def _existing_dedupe_keys(db: AsyncSession, tenant: str) -> set[tuple[str, str, str]]:
    """本租户已有通知的去重键集合 {(username, kind, ref_id)}。"""
    rows = await db.execute(
        select(Notification.username, Notification.kind, Notification.ref_id).where(
            Notification.tenant == tenant
        )
    )
    return {(row[0], row[1], row[2]) for row in rows.all()}


def _add_notification(
    bucket: list[Notification],
    seen: set[tuple[str, str, str]],
    *,
    tenant: str,
    username: str,
    kind: str,
    title: str,
    content: str,
    ref_id: str,
) -> bool:
    """按去重键并入待写通知；返回是否新增（上限由调用方控制）。"""
    key = (username, kind, ref_id)
    if key in seen:
        return False
    seen.add(key)
    bucket.append(
        Notification(
            tenant=tenant,
            username=username,
            kind=kind,
            title=title[:200],
            content=content,
            ref_id=ref_id[:64],
        )
    )
    return True


async def scan_notifications(
    db: AsyncSession,
    *,
    tenant: str,
    stale_hours: float | None = None,
    max_per_scan: int | None = None,
) -> dict[str, Any]:
    """扫描三类信号生成站内通知（幂等可重复触发）；返回创建统计。"""
    cap = int(max_per_scan if max_per_scan is not None else settings.NOTIFICATION_MAX_PER_SCAN)
    hours_limit = float(stale_hours if stale_hours is not None else settings.APPROVAL_STALE_HOURS)
    seen = await _existing_dedupe_keys(db, tenant)
    bucket: list[Notification] = []
    by_kind = {KIND_APPROVAL_STALE: 0, KIND_TASK_FAILED: 0, KIND_DAILY_BRIEFING: 0}
    stale_items: list[dict[str, Any]] = []
    skipped = 0

    # ① 审批超时：pending 且创建早于超时点 → 提醒提交人
    cutoff = _stale_cutoff(hours_limit)
    stale_rows = (
        (
            await db.execute(
                select(Approval).where(
                    Approval.tenant == tenant,
                    Approval.status == "pending",
                    Approval.created_at <= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in stale_rows:
        if len(bucket) >= cap:
            break
        hours = max(0, int((_now() - row.created_at).total_seconds() // 3600))
        created = _add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=row.applicant,
            kind=KIND_APPROVAL_STALE,
            title=f"审批单已超时 {hours} 小时未处理",
            content=(
                f"你提交的审批单「{row.action}」（{row.target}）已 {hours} 小时无人处理，"
                "请到审批中心催办或联系复核人"
            ),
            ref_id=row.id,
        )
        if created:
            by_kind[KIND_APPROVAL_STALE] += 1
            stale_items.append(
                {
                    "approval_id": row.id,
                    "action": row.action,
                    "target": row.target,
                    "applicant": row.applicant,
                    "hours": hours,
                }
            )
        else:
            skipped += 1

    # ② 失败任务：status=failed → 提醒任务提交人（逐条只提醒一次）
    failed_rows = (
        (
            await db.execute(
                select(Task).where(Task.tenant == tenant, Task.status == "failed").limit(cap * 2)
            )
        )
        .scalars()
        .all()
    )
    for row in failed_rows:
        if len(bucket) >= cap:
            break
        error_text = (row.error or "").strip()[:120] or "执行失败（无错误详情）"
        created = _add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=row.username,
            kind=KIND_TASK_FAILED,
            title=f"任务执行失败：{row.type or row.id}",
            content=f"任务 {row.id}（{row.type or '未命名'}）失败：{error_text}；可在任务页重试或转人工处理",
            ref_id=row.id,
        )
        if created:
            by_kind[KIND_TASK_FAILED] += 1
        else:
            skipped += 1

    # ③ 今日简报：按活跃用户逐人生成（ref_id=当日日期，一人一天最多一条）
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    pending_counts: dict[str, int] = {}
    counts_rows = await db.execute(
        select(Approval.applicant, func.count())
        .where(Approval.tenant == tenant, Approval.status == "pending")
        .group_by(Approval.applicant)
    )
    for applicant, count in counts_rows.all():
        pending_counts[applicant] = int(count)
    active_users = (
        (await db.execute(select(User).where(User.tenant == tenant, User.status == "active")))
        .scalars()
        .all()
    )
    for user in active_users:
        if len(bucket) >= cap:
            break
        pending = pending_counts.get(user.username, 0)
        created = _add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=user.username,
            kind=KIND_DAILY_BRIEFING,
            title=f"今日简报（{today}）",
            content=f"你名下待复核的审批单 {pending} 单；今日无事则保持关注即可",
            ref_id=today,
        )
        if created:
            by_kind[KIND_DAILY_BRIEFING] += 1
        else:
            skipped += 1

    if bucket:
        db.add_all(bucket)
        await db.commit()
    # 旁路 IM 催办：只对本轮**新增**的超时单发一次聚合提醒（重复扫描绝不刷屏）
    if stale_items:
        await im_notifier.notify_approval_stale(
            tenant=tenant, items=stale_items, stale_hours=hours_limit
        )
    total = len(bucket)
    logger.info(
        "notification scan: tenant=%s created=%d skipped=%d by_kind=%s",
        tenant,
        total,
        skipped,
        by_kind,
    )
    return {
        "created": total,
        "skipped_existing": skipped,
        "by_kind": by_kind,
        "cap": cap,
        "stale_hours": hours_limit,
    }


async def list_own_notifications(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    unread_only: bool = False,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """本人通知分页列表（最新在前）；只按 tenant+username 双过滤，跨人不可见。"""
    stmt = select(Notification).where(
        Notification.tenant == tenant, Notification.username == username
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(Notification.created_at.desc()).offset((page - 1) * size).limit(size)
            )
        )
        .scalars()
        .all()
    )
    return {"rows": list(rows), "total": int(total), "page": page, "size": size}


async def mark_read(
    db: AsyncSession, *, tenant: str, username: str, notification_id: str
) -> dict[str, Any]:
    """标记本人通知已读（幂等：已读再次调用返回原状态）。"""
    row = (
        await db.execute(
            select(Notification).where(
                Notification.tenant == tenant,
                Notification.username == username,
                Notification.id == notification_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "通知不存在或无权访问", 404)
    already = row.read_at is not None
    if not already:
        row.read_at = _now()
        await db.commit()
    return {
        "id": row.id,
        "already_read": already,
        "read_at": row.read_at.isoformat(sep=" ", timespec="seconds") if row.read_at else "",
    }
