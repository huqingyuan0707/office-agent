"""运行步骤时间线（Run 复用 server 的 Task 表，步骤明细归本表）

链路：runner 主循环每执行/失败一步 → 写一行 RunStep（args / 结果摘要 / trace）
      → GET /runs/{id} 按 (step_index, created_at) 读出，前端时间线直接渲染。

口径：挂 server 的 Base metadata（office_agent_server.db.Base），随壳层 lifespan 的
     init_models() 一起建表，runtime 不带独立迁移——前提是 mount(app) 发生在
     宿主 startup 之前（见 ext.py）。JSON 一律 Text 存文本（SQLite 无 JSONB）；
     时间 naive UTC；主键 String(32) uuid hex，与 server/models.py 同一套约定。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from office_agent_server.db import Base, _now, _uid


class RunStep(Base):
    """一次智能体运行的步骤留痕（一 run 多步；失败重试会留下多行同 step_index）。"""

    __tablename__ = "run_steps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    tenant: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    tool: Mapped[str] = mapped_column(String(64), default="")
    args: Mapped[str] = mapped_column(Text, default="{}")
    # 结果摘要：存工具返回 JSON 文本（截断防膨胀；完整结果在 Task.checkpoint 里供续跑取值）
    result_digest: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    #: 规划来源：planner 用 LLM 提议 = "llm"，用规则模板提议 = "rule"（可观测「这步是哪种规划出来的」）
    planner_source: Mapped[str] = mapped_column(String(8), default="rule", index=True)
    approval_id: Mapped[str] = mapped_column(String(32), default="")
    trace_id: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)
