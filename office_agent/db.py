"""数据库层：async engine + tasks / approvals / audit_logs 三张基础表 + init_db。

职责：
- 声明 async engine 与 session 工厂（SQLite + aiosqlite，单文件零部署）；
- 三张基础表：tasks（工具执行记录）、approvals（审批单）、audit_logs（审计日志，只追加）；
- init_db()：启动期 create_all 建表（幂等）；
- record_audit / record_task / list_tasks：审计与任务落库的小工具函数，executor 与 approvals 共用同一口径。
链路：main.lifespan → init_db()；路由经 get_db 依赖取 session → executor / approvals 写表 → 路由读 /tasks、/approvals。
对齐：docs/office-agent仓库骨架与内核提取方案.md §3（db/models_foundation 的 Task/ApprovalLog/审计表 → server）。
"""

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from office_agent.config import settings


def utcnow() -> datetime:
    """当前 UTC 时间（去时区存储，SQLite 友好）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """声明式基类。"""


class Task(Base):
    """tasks 表：工具执行记录（status: succeeded/failed；MVP 内每次执行完成时 progress=100）。"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="succeeded")
    progress: Mapped[int] = mapped_column(default=100)
    result_json: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Approval(Base):
    """approvals 表：审批单（status: pending/approved/rejected；同人审批由服务层 1001 拦截）。"""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(100))
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    requested_by: Mapped[str] = mapped_column(String(100))
    decided_by: Mapped[str | None] = mapped_column(String(100), default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class AuditLog(Base):
    """audit_logs 表：全链路审计日志（只追加、不修改、不删除）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    trace_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


engine = create_async_engine(settings.OFFICE_DB_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """建表（create_all 幂等，已存在则跳过）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个独立 session，用完即关。"""
    async with SessionLocal() as session:
        yield session


async def record_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    detail: dict,
    trace_id: str | None = None,
) -> None:
    """写一条审计日志并提交（executor / approvals 全分支共用）。"""
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            detail_json=json.dumps(detail, ensure_ascii=False),
            trace_id=trace_id,
            created_at=utcnow(),
        )
    )
    await session.commit()


async def record_task(
    session: AsyncSession,
    *,
    type_: str,
    status: str,
    created_by: str,
    result: dict | None = None,
    error: str | None = None,
) -> Task:
    """落一条任务执行记录并提交，返回该行（直接执行与审批批准后执行共用）。"""
    task = Task(
        type=type_,
        status=status,
        progress=100,
        result_json=json.dumps(result, ensure_ascii=False) if result is not None else None,
        error=error,
        created_by=created_by,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(task)
    await session.commit()
    return task


async def list_tasks(session: AsyncSession, limit: int = 100) -> list[Task]:
    """任务列表（最新在前）。"""
    rows = await session.execute(select(Task).order_by(Task.id.desc()).limit(limit))
    return list(rows.scalars())
