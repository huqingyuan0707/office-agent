"""通知信号收集器（纯内存聚合，无 DB/网络）：简报 + 个人事务四类信号的待写通知构建。

职责：
- add_notification()：按去重键 (username, kind, ref_id) 并入待写通知桶（扫描主链与各收集器共用）；
- collect_daily_briefing()：③ 今日简报逐人生成（审批数 + 事务存储实测的今日待办/会议计数）；
- collect_affairs_signals()：④~⑦ 待办到期 / 会议临近 / 项目节点 / 周五周报提示
  （PRD §2.2 智能主动推送），四类各自独立收集器保可读性与复杂度。
时间口径：全部用业务时区 naive datetime 与 affairs 存储原值比对（坏值跳过不炸整轮）。
对齐：AGENTS.md §3（分层红线：业务进 services/纯函数）；智能办公Agent 产品需求文档.md §5.1、§2.2。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from office_agent_core.settings import settings
from office_agent_server.models import Notification
from office_agent_server.services.notification_kinds import (
    KIND_DAILY_BRIEFING,
    KIND_MEETING_UPCOMING,
    KIND_MILESTONE_ALERT,
    KIND_TODO_DUE,
    KIND_WEEKLY_DRAFT_HINT,
)


def _safe_date(text: Any) -> date | None:
    """宽松解析 YYYY-MM-DD；坏值返回 None（扫描链跳过该记录，不炸整轮）。"""
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def _safe_dt(text: Any) -> datetime | None:
    """宽松解析 YYYY-MM-DD HH:MM；坏值返回 None。"""
    try:
        return datetime.strptime(str(text or "").strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def add_notification(
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


def _collect_todo_due(
    affairs_data: dict[str, Any],
    biz_now: datetime,
    seen: set[tuple[str, str, str]],
    bucket: list[Notification],
    tenant: str,
    cap: int,
    by_kind: dict[str, int],
) -> int:
    """④ 任务到期提醒：open 待办且截止日 ≤ 现在+提前窗口（含逾期），提醒 owner。返回去重跳过数。"""
    skipped = 0
    due_cutoff = (biz_now + timedelta(hours=settings.TODO_DUE_LOOKAHEAD_HOURS)).date()
    for t in affairs_data["todos"]:
        if len(bucket) >= cap:
            break
        if t.get("status") != "open" or not t.get("due_date"):
            continue
        due = _safe_date(t["due_date"])
        owner = str(t.get("owner") or "")
        if due is None or not owner or due > due_cutoff:
            continue
        overdue = due < biz_now.date()
        created = add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=owner,
            kind=KIND_TODO_DUE,
            title=("待办已逾期" if overdue else "待办临近截止"),
            content=(
                f"待办「{t.get('title', '')}」截止 {t['due_date']}"
                f"{'（已逾期，建议改期或尽快处理）' if overdue else '（请安排处理）'}；"
                "可用对话说「把待办改期」或直接 todo.update"
            ),
            ref_id=str(t.get("id", "")),
        )
        if created:
            by_kind[KIND_TODO_DUE] += 1
        else:
            skipped += 1
    return skipped


def _collect_meeting_upcoming(
    affairs_data: dict[str, Any],
    biz_now: datetime,
    seen: set[tuple[str, str, str]],
    bucket: list[Notification],
    tenant: str,
    cap: int,
    by_kind: dict[str, int],
) -> int:
    """⑤ 会议临近通知：窗口内开始的会议，创建人与每位参会人各一条（站内口径，不假发外部邮件）。"""
    skipped = 0
    meet_cutoff = biz_now + timedelta(hours=settings.MEETING_UPCOMING_HOURS)
    for s in affairs_data["schedules"]:
        if len(bucket) >= cap:
            break
        if s.get("kind") != "meeting":
            continue
        start_dt = _safe_dt(s.get("start", ""))
        if start_dt is None or not (biz_now < start_dt <= meet_cutoff):
            continue
        minutes = max(0, int((start_dt - biz_now).total_seconds() // 60))
        targets = [str(s.get("owner") or "")] + [str(a) for a in (s.get("attendees") or [])]
        for person in dict.fromkeys(p for p in targets if p):
            if len(bucket) >= cap:
                break
            created = add_notification(
                bucket,
                seen,
                tenant=tenant,
                username=person,
                kind=KIND_MEETING_UPCOMING,
                title=f"会议临近：{s.get('title', '')}",
                content=(
                    f"「{s.get('title', '')}」{s.get('start', '')} 开始（约 {minutes} 分钟后），"
                    f"地点 {s.get('location') or '未指定'}；可先出议程（office.meeting.agenda）"
                ),
                ref_id=str(s.get("id", "")),
            )
            if created:
                by_kind[KIND_MEETING_UPCOMING] += 1
            else:
                skipped += 1
    return skipped


def _collect_milestone_alert(
    affairs_data: dict[str, Any],
    biz_now: datetime,
    seen: set[tuple[str, str, str]],
    bucket: list[Notification],
    tenant: str,
    cap: int,
    by_kind: dict[str, int],
) -> int:
    """⑥ 项目节点预警：N 天内到的 milestone，提醒创建人。返回去重跳过数。"""
    skipped = 0
    ms_cutoff = biz_now.date() + timedelta(days=int(settings.MILESTONE_LOOKAHEAD_DAYS))
    for s in affairs_data["schedules"]:
        if len(bucket) >= cap:
            break
        if s.get("kind") != "milestone":
            continue
        ms_day = _safe_date(str(s.get("start", ""))[:10])
        owner = str(s.get("owner") or "")
        if ms_day is None or not owner or not (biz_now.date() <= ms_day <= ms_cutoff):
            continue
        created = add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=owner,
            kind=KIND_MILESTONE_ALERT,
            title=f"项目节点临近：{s.get('title', '')}",
            content=(
                f"节点「{s.get('title', '')}」定于 {str(s.get('start', ''))[:10]}"
                f"（还剩 {(ms_day - biz_now.date()).days} 天），请确认前置事项是否就绪"
            ),
            ref_id=str(s.get("id", "")),
        )
        if created:
            by_kind[KIND_MILESTONE_ALERT] += 1
        else:
            skipped += 1
    return skipped


def _collect_weekly_hint(
    biz_now: datetime,
    today: str,
    active_usernames: list[str],
    seen: set[tuple[str, str, str]],
    bucket: list[Notification],
    tenant: str,
    cap: int,
    by_kind: dict[str, int],
) -> int:
    """⑦ 周五主动提示：业务时区周五，提醒活跃用户可让 Agent 出周报草稿。返回去重跳过数。"""
    if biz_now.weekday() != 4:
        return 0
    skipped = 0
    for username in active_usernames:
        if len(bucket) >= cap:
            break
        created = add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=username,
            kind=KIND_WEEKLY_DRAFT_HINT,
            title="周五了，该出周报草稿了",
            content=(
                "本周工作可一键汇总：对我说「生成本周周报」即可"
                "（office.report.generate，台账见 office.worklog.generate）"
            ),
            ref_id=today,
        )
        if created:
            by_kind[KIND_WEEKLY_DRAFT_HINT] += 1
        else:
            skipped += 1
    return skipped


def collect_affairs_signals(
    *,
    affairs_data: dict[str, Any],
    biz_now: datetime,
    today: str,
    active_usernames: list[str],
    seen: set[tuple[str, str, str]],
    bucket: list[Notification],
    tenant: str,
    cap: int,
    by_kind: dict[str, int],
) -> int:
    """个人事务四类信号（④到期/⑤会前/⑥节点/⑦周五周报提示）并入待写通知；返回去重跳过数。"""
    return (
        _collect_todo_due(affairs_data, biz_now, seen, bucket, tenant, cap, by_kind)
        + _collect_meeting_upcoming(affairs_data, biz_now, seen, bucket, tenant, cap, by_kind)
        + _collect_milestone_alert(affairs_data, biz_now, seen, bucket, tenant, cap, by_kind)
        + _collect_weekly_hint(biz_now, today, active_usernames, seen, bucket, tenant, cap, by_kind)
    )


def _affairs_today_counts(
    affairs_data: dict[str, Any], username: str, today: str
) -> tuple[int, int]:
    """本人「今日到期/逾期待办数、今日会议数」（简报口径，事务存储实测）。"""
    n_todo = sum(
        1
        for t in affairs_data["todos"]
        if t.get("owner") == username
        and t.get("status") == "open"
        and t.get("due_date")
        and str(t["due_date"]) <= today
    )
    n_meet = sum(
        1
        for s in affairs_data["schedules"]
        if s.get("kind") == "meeting"
        and str(s.get("start", "")).startswith(today)
        and (s.get("owner") == username or username in (s.get("attendees") or []))
    )
    return n_todo, n_meet


def collect_daily_briefing(
    affairs_data: dict[str, Any] | None,
    today: str,
    pending_counts: dict[str, int],
    active_usernames: list[str],
    seen: set[tuple[str, str, str]],
    bucket: list[Notification],
    tenant: str,
    cap: int,
    by_kind: dict[str, int],
) -> int:
    """③ 今日简报逐人生成：审批数 +（可选装配时）今日待办/会议实测计数。返回去重跳过数。"""
    skipped = 0
    for username in active_usernames:
        if len(bucket) >= cap:
            break
        brief = f"你名下待复核的审批单 {pending_counts.get(username, 0)} 单"
        if affairs_data is not None:
            n_todo, n_meet = _affairs_today_counts(affairs_data, username, today)
            brief += f"；今日到期/逾期待办 {n_todo} 件；今日会议 {n_meet} 场"
        created = add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=username,
            kind=KIND_DAILY_BRIEFING,
            title=f"今日简报（{today}）",
            content=brief + "；无事则保持关注即可",
            ref_id=today,
        )
        if created:
            by_kind[KIND_DAILY_BRIEFING] += 1
        else:
            skipped += 1
    return skipped
