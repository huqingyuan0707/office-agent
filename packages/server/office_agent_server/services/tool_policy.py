"""细粒度权限服务：按角色启用/禁用工具（PRD §2.11「工具细粒度权限管控」/ §4.1）。

链路：api/admin（管理面 CRUD） → 本模块 → tool_policies 表；
      执行面四处拦截（工具清单过滤 / invoke 预检 / Agent 步骤预检 / 审批放行复核）
      → 本模块 assert_tool_enabled → BusinessError(4006)。

口径（与 core/policy.py 的 Scope 判定互补，不重复实现）：
- Scope 管「有没有这个工具的权限」，本模块管「这个角色能不能用这个工具」；
- 默认放行：没有策略行 = 允许（存量行为零变化，不配置即不生效）；
- 通配角色豁免：持 ``*`` 角色的超级用户不受任何策略约束（管理员不会被自己的策略锁死）；
- ``role="*"`` 的策略行对所有非通配用户生效（全局禁用一个危险工具的口子）；
- 角色必须真实存在（取本租户用户实际持有的角色），杜绝「拼错角色名静默不生效」。

红线：薄封装端点只做入参解析 + 调本模块 + ok()/fail()；业务判定与落库全在本模块；
      策略只影响「能不能调」，不改工具语义、不绕过审批闸门。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import registry
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.models import ToolPolicy, User
from office_agent_server.security import split_roles

#: 通配角色（既表示「全部 Scope」也表示「超级用户」）
WILDCARD_ROLE = "*"

#: 角色名与工具名的长度上限（与表列宽一致，避免超长写入被数据库截断）
_ROLE_MAX = 64
_TOOL_MAX = 64


def _clean(value: Any, field: str) -> str:
    """入参清洗：去空白 + 长度校验（空值给可操作提示，不静默通过）。"""
    text = str(value or "").strip()
    if not text:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 {field} 不能为空")
    if len(text) > (_ROLE_MAX if field == "role" else _TOOL_MAX):
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"参数 {field} 长度不能超过 {_ROLE_MAX if field == 'role' else _TOOL_MAX}",
        )
    return text


async def tenant_roles(db: AsyncSession, tenant: str) -> list[str]:
    """本租户真实持有的角色清单（用户表 roles 逗号分隔拆分去重）。

    供管理面下拉与「角色必须存在」校验使用——角色名拼错时立刻报 1001，
    而不是落一条永远不生效的策略。
    """
    rows = (await db.execute(select(User.roles).where(User.tenant == tenant))).scalars().all()
    names: set[str] = set()
    for raw in rows:
        names.update(split_roles(raw))
    return sorted(names)


async def disabled_tools(db: AsyncSession, *, tenant: str, roles: list[str]) -> set[str]:
    """当前角色集下被禁用的工具名集合（判定唯一出处）。

    通配角色（``*``）豁免：超级用户始终可用全部工具（逃生通道）。
    """
    role_set = {role for role in roles if role}
    if not role_set or WILDCARD_ROLE in role_set:
        return set()
    stmt = select(ToolPolicy.tool_name).where(
        ToolPolicy.tenant == tenant,
        ToolPolicy.enabled == 0,
        ToolPolicy.role.in_(role_set | {WILDCARD_ROLE}),
    )
    return {str(name) for name in (await db.execute(stmt)).scalars().all()}


async def assert_tool_enabled(
    db: AsyncSession, *, tenant: str, roles: list[str], tool_name: str
) -> None:
    """工具被该角色禁用 → 直接抛 4006（可操作提示：联系管理员开通）。"""
    if tool_name in await disabled_tools(db, tenant=tenant, roles=roles):
        raise BusinessError(
            ErrorCode.TOOL_SCOPE_DENIED,
            f"工具 {tool_name} 已被管理员对该角色禁用（细粒度权限策略），如需使用请联系管理员开通",
            403,
        )


async def list_policies(db: AsyncSession, *, tenant: str) -> list[dict[str, Any]]:
    """策略清单（管理面只读视图：角色 / 工具 / 启停状态）。"""
    rows = (
        (
            await db.execute(
                select(ToolPolicy)
                .where(ToolPolicy.tenant == tenant)
                .order_by(ToolPolicy.role, ToolPolicy.tool_name)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "role": row.role,
            "tool_name": row.tool_name,
            "enabled": bool(row.enabled),
            "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds"),
        }
        for row in rows
    ]


async def upsert_policy(
    db: AsyncSession, *, tenant: str, role: str, tool_name: str, enabled: bool
) -> dict[str, Any]:
    """写入/更新一条策略（同角色同工具唯一，重复提交即更新，不产生重复行）。"""
    role_name = _clean(role, "role")
    tool = _clean(tool_name, "tool_name")
    if registry.maybe_get(tool) is None:
        raise BusinessError(
            ErrorCode.TOOL_NOT_FOUND, f"工具不存在：{tool}（策略只能针对已注册工具设置）"
        )
    known = await tenant_roles(db, tenant)
    if role_name != WILDCARD_ROLE and role_name not in known:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"角色不存在：{role_name}（本租户现有角色：{'、'.join(known) or '（无）'}）",
        )
    row = (
        await db.execute(
            select(ToolPolicy).where(
                ToolPolicy.tenant == tenant,
                ToolPolicy.role == role_name,
                ToolPolicy.tool_name == tool,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ToolPolicy(tenant=tenant, role=role_name, tool_name=tool, enabled=1 if enabled else 0)
        db.add(row)
    else:
        row.enabled = 1 if enabled else 0
    await db.commit()
    await db.refresh(row)
    return {
        "id": row.id,
        "role": row.role,
        "tool_name": row.tool_name,
        "enabled": bool(row.enabled),
    }


async def delete_policy(
    db: AsyncSession, *, tenant: str, role: str, tool_name: str
) -> dict[str, Any]:
    """删除一条策略（回到默认放行）；策略不存在时如实回 deleted=False，不报错。"""
    role_name = _clean(role, "role")
    tool = _clean(tool_name, "tool_name")
    result = await db.execute(
        delete(ToolPolicy).where(
            ToolPolicy.tenant == tenant,
            ToolPolicy.role == role_name,
            ToolPolicy.tool_name == tool,
        )
    )
    await db.commit()
    return {
        "role": role_name,
        "tool_name": tool,
        "deleted": bool(result.rowcount),
        "note": "策略已删除，该角色恢复默认放行"
        if result.rowcount
        else "策略本就不存在（该角色默认放行）",
    }
