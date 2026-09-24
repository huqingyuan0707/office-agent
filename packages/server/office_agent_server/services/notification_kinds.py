"""站内通知类型常量与中文标签（kind 的唯一出处，扫描链与展示层共用）。

链路：services/notifications.py（扫描）/ services/notification_signals.py（信号收集）/
      api/notifications.py（kind_label 展示）统一引用本模块，避免字符串散落三份。
对齐：AGENTS.md §3（分层红线：业务进 services/）；智能办公Agent 产品需求文档.md §5.1、§2.2。
"""

from __future__ import annotations

KIND_APPROVAL_STALE = "approval_stale"
KIND_TASK_FAILED = "task_failed"
KIND_DAILY_BRIEFING = "daily_briefing"
KIND_TODO_DUE = "todo_due"
KIND_MEETING_UPCOMING = "meeting_upcoming"
KIND_MILESTONE_ALERT = "milestone_alert"
KIND_WEEKLY_DRAFT_HINT = "weekly_draft_hint"

#: 通知类型中文名（展示层唯一出处）
KIND_LABELS: dict[str, str] = {
    KIND_APPROVAL_STALE: "审批超时提醒",
    KIND_TASK_FAILED: "任务失败提醒",
    KIND_DAILY_BRIEFING: "今日简报",
    KIND_TODO_DUE: "待办到期提醒",
    KIND_MEETING_UPCOMING: "会议临近通知",
    KIND_MILESTONE_ALERT: "项目节点预警",
    KIND_WEEKLY_DRAFT_HINT: "周报草稿提示",
}
