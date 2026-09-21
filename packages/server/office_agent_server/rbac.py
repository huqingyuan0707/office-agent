"""认证鉴权依赖（路由级登录 + 敏感鉴权）

链路：OAuth2 Bearer → 解析 Token → 构造 CurrentUser → 端点用 Depends 取人。

口径「角色即权限」：Token 的 roles 里既放域角色也放 Scope 令牌，
与内核 ``policy`` 的 Scope 判定同源（内核侧唯一出处是 ``policy.has_scope``，此处不另写一份）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from office_agent_server.security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


@dataclass(frozen=True)
class CurrentUser:
    """Token 解析后的最小用户信息，不含密钥。"""

    username: str
    tenant: str
    roles: list[str]


async def get_current_user(token: str = Depends(oauth2_scheme)) -> CurrentUser:
    """登录依赖：验签解 JWT → 构造 CurrentUser（不取请求体里的身份字段）。"""
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        claims = decode_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    roles = claims.get("roles")
    user = CurrentUser(
        username=str(claims.get("sub", "")),
        tenant=str(claims.get("tenant", "")),
        roles=list(roles) if isinstance(roles, list) else [],
    )
    if not user.username or not user.tenant:
        raise HTTPException(status_code=401, detail="Token 非法")
    return user


def require_perm(perm: str) -> Callable[..., Awaitable[CurrentUser]]:
    """敏感端点二次鉴权，如 ``Depends(require_perm("office:write"))``。"""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if perm not in user.roles and "*" not in user.roles:
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return _check


def require_any_perm(*perms: str) -> Callable[..., Awaitable[CurrentUser]]:
    """任一权限命中即放行（同一动作允许多个角色，与 require_perm 同口径）。"""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if "*" in user.roles or any(perm in user.roles for perm in perms):
            return user
        raise HTTPException(status_code=403, detail="权限不足")

    return _check