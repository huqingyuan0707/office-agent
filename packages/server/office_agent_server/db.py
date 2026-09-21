"""数据库会话（引擎单例 + 建表）

链路：init_models() 建表 → get_db() 取会话 → 端点查询 / 审计落库。
生产切 PG 只需换 ``DATABASE_URL``（Settings），模型层无需改动。

口径：主键 String(32) uuid hex；时间 naive UTC（与 SQLite 落库口径一致）。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from office_agent_core.settings import settings


class Base(DeclarativeBase):
    """声明式基类（被 models 共享）。"""


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    """naive UTC（datetime.utcnow() 在 3.12+ 已弃用，故显式转换）。"""
    return datetime.now(UTC).replace(tzinfo=None)


_engine = None
_engine_lock = threading.Lock()
_SessionFactory: async_sessionmaker[AsyncSession] | None = None


def get_engine():  # type: ignore[no-untyped-def]
    """惰性单例引擎（双检锁，进程内唯一）。"""
    global _engine, _SessionFactory
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = create_async_engine(settings.DATABASE_URL, future=True)
                _SessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    assert _SessionFactory is not None
    return _engine


async def init_models() -> None:
    """建表（幂等）。Alembic 接入后此处仅保留应急建表。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI Depends 用的会话生成器（读写失败由调用方转 fail）。"""
    async with session_factory()() as session:
        yield session


def session_factory() -> async_sessionmaker[AsyncSession]:
    """非 Depends 场景的会话工厂（启动种子、后台任务）。"""
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


async def dispose_engine() -> None:
    """释放连接池（测试与关停用）。"""
    global _engine, _SessionFactory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _SessionFactory = None