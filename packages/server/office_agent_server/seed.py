"""演示种子账号（按 Settings 幂等补齐，生产经 ENV=prod + SEED_ON_START=false 关闭）

链路：lifespan 启动 → seed_on_startup() → 无则建号，有则只并集补角色（绝不覆盖存量口令）。
      → seed_reviewer() → 额外建一个复核员账号（approver 角色，专审批；与 admin 双角色分离）。

为什么「只补不覆盖」：口令改过之后重启不应被种子打回默认值，
但插件加进来引入新 Scope 令牌时，老账号需要被补上才能调得动新工具。

复核员种子：独立于 admin，REVIEWER_USERNAME/REVIEWER_PASSWORD 环境变量可覆盖，
默认 reviewer/reviewer123；审批闭环要求「审批人 != 提交人」，所以需要两个独立账号。
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select

from office_agent_core.settings import settings
from office_agent_server.db import session_factory
from office_agent_server.models import User
from office_agent_server.security import hash_password, split_roles

logger = logging.getLogger(__name__)

#: 复核员默认凭据（可经环境变量覆盖）
_DEFAULT_REVIEWER_USERNAME = "reviewer"
_DEFAULT_REVIEWER_PASSWORD = "reviewer123"
#: 复核员角色：admin + approver + office:read（催办代管按「本人或管理员」放行；
#: office:read 让复核员用得动 draft/check/opinion 等审批辅助只读工具，写口径仍不给）
_REVIEWER_ROLES = ("admin", "approver", "office:read")


def _merge_roles(existing: str, wanted: list[str]) -> str:
    """并集补齐角色（保持原顺序，新令牌追加在后）。"""
    current = split_roles(existing)
    merged = current + [role for role in wanted if role not in current]
    return settings.ROLES_SEPARATOR.join(merged)


async def _ensure_user(
    *,
    session,
    username: str,
    password: str,
    tenant: str,
    roles: list[str],
    label: str = "",
) -> None:
    """幂等建/补单个账号：无则建，有则只并集补角色（绝不覆盖存量口令）。"""
    row = (
        await session.execute(select(User).where(User.tenant == tenant, User.username == username))
    ).scalar_one_or_none()
    if row is None:
        session.add(
            User(
                tenant=tenant,
                username=username,
                pwd_hash=hash_password(password),
                roles=settings.ROLES_SEPARATOR.join(roles),
            )
        )
        logger.info("已建%s种子账号：%s@%s", label, username, tenant)
    else:
        merged = _merge_roles(row.roles, roles)
        if merged != row.roles:
            row.roles = merged
            logger.info("已为%s种子账号补齐角色：%s", label, username)


async def seed_on_startup() -> str:
    """幂等建/补全部种子账号，返回主种子账号名（未开启种子返回空串）。"""
    if not settings.SEED_ON_START:
        return ""

    admin_roles = split_roles(settings.SEED_ROLES)
    reviewer_username = os.environ.get("REVIEWER_USERNAME", _DEFAULT_REVIEWER_USERNAME).strip()
    reviewer_password = os.environ.get("REVIEWER_PASSWORD", _DEFAULT_REVIEWER_PASSWORD)

    factory = session_factory()
    async with factory() as session:
        await _ensure_user(
            session=session,
            username=settings.SEED_USERNAME,
            password=settings.SEED_PASSWORD.get_secret_value(),
            tenant=settings.SEED_TENANT,
            roles=admin_roles,
            label="",
        )
        if reviewer_username and reviewer_password:
            await _ensure_user(
                session=session,
                username=reviewer_username,
                password=reviewer_password,
                tenant=settings.SEED_TENANT,
                roles=list(_REVIEWER_ROLES),
                label="复核员",
            )
        await session.commit()
    return settings.SEED_USERNAME
