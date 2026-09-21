"""演示种子账号（按 Settings 幂等补齐，生产经 ENV=prod + SEED_ON_START=false 关闭）

链路：lifespan 启动 → seed_on_startup() → 无则建号，有则只并集补角色（绝不覆盖存量口令）。

为什么「只补不覆盖」：口令改过之后重启不应被种子打回默认值，
但插件加进来引入新 Scope 令牌时，老账号需要被补上才能调得动新工具。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from office_agent_core.settings import settings
from office_agent_server.db import session_factory
from office_agent_server.models import User
from office_agent_server.security import hash_password, split_roles

logger = logging.getLogger(__name__)


def _merge_roles(existing: str, wanted: list[str]) -> str:
    """并集补齐角色（保持原顺序，新令牌追加在后）。"""
    current = split_roles(existing)
    merged = current + [role for role in wanted if role not in current]
    return settings.ROLES_SEPARATOR.join(merged)


async def seed_on_startup() -> str:
    """幂等建/补种子账号，返回账号名（未开启种子返回空串）。"""
    if not settings.SEED_ON_START:
        return ""
    wanted = split_roles(settings.SEED_ROLES)
    factory = session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(User).where(
                    User.tenant == settings.SEED_TENANT,
                    User.username == settings.SEED_USERNAME,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                User(
                    tenant=settings.SEED_TENANT,
                    username=settings.SEED_USERNAME,
                    pwd_hash=hash_password(settings.SEED_PASSWORD.get_secret_value()),
                    roles=settings.ROLES_SEPARATOR.join(wanted),
                )
            )
            logger.info("已建演示种子账号：%s@%s", settings.SEED_USERNAME, settings.SEED_TENANT)
        else:
            merged = _merge_roles(row.roles, wanted)
            if merged != row.roles:
                row.roles = merged
                logger.info("已为种子账号补齐角色：%s", row.username)
        await session.commit()
    return settings.SEED_USERNAME