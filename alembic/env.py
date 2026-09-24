"""Alembic 迁移环境（异步引擎 + 双包模型全量注册）

链路：alembic upgrade head → 本文件读 Settings.DATABASE_URL（与业务同源，零双源配置）
→ async 引擎 + run_sync 执行迁移。

口径：持久库 schema 演进**唯一入口是本迁移链**（create_all 不做 ALTER，存量表加列
必须走迁移，dev 库缺列事故即此因）；测试/内存库仍走 init_models 的 create_all，互不干扰。

红线：RunStep 挂在 server 的 Base 上，故 versions autogenerate 前必须同时 import
server 与 runtime 的 models——漏一个即漏表。
"""

from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# 全量模型注册（缺一即漏表）：server 地基表 + runtime run_steps
import office_agent_runtime.models  # noqa: F401
import office_agent_server.models  # noqa: F401
from alembic import context

# DATABASE_URL 唯一出处：内核 Settings（.env / 环境变量双读，零硬编码）
from office_agent_core.settings import settings
from office_agent_server.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL 注入 alembic 配置节（ini 里刻意留空，防双源漂移）
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不连库。"""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：async 引擎建连，run_sync 桥接 alembic 同步迁移执行。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_async_migrations())
