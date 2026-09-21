"""认证端点（真实验密发 JWT）

链路：POST /auth/login → 查库验密 → 签发 JWT → ok({token, user})。
端点只解析入参 + 组信封，验密与签发在 security/本文件的小函数里，不散落别处。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import ErrorCode
from office_agent_server.db import get_db
from office_agent_server.models import User
from office_agent_server.rbac import CurrentUser
from office_agent_server.responses import fail, ok
from office_agent_server.security import issue_token, split_roles, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录入参（端点私有 DTO）。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


def _user_payload(user: CurrentUser) -> dict[str, Any]:
    """当前用户投影（不含任何密钥字段）。"""
    return {"name": user.username, "tenant": user.tenant, "roles": user.roles}


@router.post("/login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> object:
    """验密：失败统一 401/1002 中文提示（不区分账号不存在与密码错，防账号探测）。"""
    row = (
        await db.execute(select(User).where(User.username == payload.username.strip()))
    ).scalar_one_or_none()
    if row is None or not verify_password(payload.password, row.pwd_hash):
        return fail(ErrorCode.UNAUTHORIZED, "用户名或密码错误", 401)
    # 冻结校验排在验密之后：否则「账号不存在」与「已冻结」的提示差异会变成账号探测器
    if (row.status or "active") != "active":
        return fail(ErrorCode.UNAUTHORIZED, "该账号已被冻结，请联系管理员解冻", 401)
    user = CurrentUser(username=row.username, tenant=row.tenant, roles=split_roles(row.roles))
    return ok(
        {"token": issue_token(user.username, user.tenant, user.roles), "user": _user_payload(user)},
        "登录成功",
    )
