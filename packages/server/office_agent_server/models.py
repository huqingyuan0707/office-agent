"""数据模型（用户 / 任务 / 工具调用审计 / 审批）

链路：alembic 或 init_models() 建表 → 端点按 tenant 读写 → 审计 sink 落 tool_calls。
口径：业务表必带 tenant 字符串；JSON 一律 Text 存 JSON 文本（SQLite 无 JSONB）；
      时间 naive UTC；主键 String(32) uuid hex。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from office_agent_server.db import Base, _now, _uid


class Notification(Base):
    """站内通知（主动消息推送的落库位：扫描生成 + 已读回执，内容生成后不篡改）。

    去重口径：(tenant, username, kind, ref_id) 唯一——同一事项同一人只提醒一次；
    ref_id 按语义取值：审批单 id / 任务 id / 简报日期（YYYY-MM-DD）。
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("tenant", "username", "kind", "ref_id", name="uq_notify_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    tenant: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    ref_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    read_at: Mapped[datetime | None] = mapped_column(default=None)


class User(Base):
    """登录用户（租户内用户名唯一，角色逗号分隔存文本）。

    status：active 正常 / frozen 冻结（冻结后直接拒登，但**行保留**——
    审计与任务仍需回溯到人，与「删账号」是两件事）。
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant", "username", name="uq_users_tenant_username"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    tenant: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    pwd_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    roles: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Task(Base):
    """异步任务（长任务提交/轮询/检查点，状态机 pending/running/done/failed）。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    tenant: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(48), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0)
    input: Mapped[str] = mapped_column(Text, default="{}")
    output: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="{}")
    checkpoint: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class ToolCall(Base):
    """工具调用审计（谁/何时/调什么/结果/耗时，好路径与坏路径都留痕）。

    result 存完整出参 JSON——远程工具的出参含 ``provenance``（数据出处），
    所以「这份数据从哪来、什么时候取的」在审计里天然可查。
    """

    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    trace_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="", index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(64), default="", index=True)
    args: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[str] = mapped_column(Text, default="{}")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Approval(Base):
    """审批单（写动作恒送审的落库位）。

    M2 起由需审批工具的本地实现写入「生效所需参数」，审批通过后由生效动作消费；
    本阶段（只读工具）该表只承载读口径与批/驳状态流转，不伪造业务生效结果。
    """

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    tenant: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(40), default="")
    action: Mapped[str] = mapped_column(String(48), index=True)
    target: Mapped[str] = mapped_column(String(120), default="")
    args: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(String(200), default="")
    applicant: Mapped[str] = mapped_column(String(64), default="")
    approver: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)


class Workflow(Base):
    """可视化编排的工作流定义（多场景串联的落库位）。

    steps 存 JSON 数组 ``[{tool, args}]``（上限 20 步，保存与执行前双重校验）；
    执行按序调内核 executor——需审批步骤只落单即停（pending_approval + 后续步骤不跑），
    远程工具步骤走同一条联动出站（含 provenance 溯源），即 RPA 通路的本仓库形态。
    """

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    tenant: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(200), default="")
    steps: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class ScheduledJob(Base):
    """定时任务定义（PRD §2.11 高阶自动化·定时任务的落库位）。

    调度口径：schedule 存 JSON（``{"kind":"daily","at":"08:30"}`` 业务时区 /
    ``{"kind":"weekly","day":0-6,"at":"HH:MM"}`` / ``{"kind":"interval","seconds":N}``），
    next_run_at 存换算后的 naive UTC——调度环只比对一个时间列，不做每轮解析；
    job_type 白名单（notify_scan 通知扫描链 / workflow_run 编排执行），
    payload 存执行参数（workflow_run 必带 workflow_id）；
    执行身份 = created_by（运行时查库取实时角色，Scope 硬拦绕不过）。
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    tenant: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    schedule: Mapped[str] = mapped_column(Text, default="{}")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    next_run_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(default=None)
    last_result: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
