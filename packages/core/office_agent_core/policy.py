"""工具策略引擎（Scope 鉴权 + 敏感动作恒进审批）

链路：executor.call 执行前必过 ensure_allowed() → {"allowed","approval_required","reason"}。

红线：
- Scope 不命中直接拒（4006），绝不「没权限也执行」；角色即权限，``*`` 通配。
- requires_approval=True 的敏感工具：调用即落审批单，账不动，**不看金额/参数走分支**
  （恒送审口径；一旦按参数分支，就出现「改个参数绕过审批」的口子）。
"""

from __future__ import annotations

from typing import Any

from office_agent_core.contracts import ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode


def has_scope(roles: list[str], *wanted: str) -> bool:
    """Scope 校验：角色即权限口径，命中任一即放行，``*`` 通配。

    唯一出处：内置工具与远程工具共用同一套判定，宿主 RBAC 也不另写一份。
    """
    if "*" in roles:
        return True
    return any(scope in roles for scope in wanted)


def check(*, roles: list[str], spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """策略判定：先 Scope（硬拦），再判是否需审批（软分支）。

    args 保留入参位（后续按数值/密级做细粒度策略），当前不据此放行或拦截，
    避免出现「同一工具因参数不同而绕过 Scope」的口子。
    """
    _ = args
    if not has_scope(roles, spec.scope):
        return {
            "allowed": False,
            "approval_required": False,
            "scope": spec.scope,
            "reason": f"缺少 {spec.scope} 权限，无法调用工具 {spec.name}",
        }
    if spec.requires_approval:
        return {
            "allowed": True,
            "approval_required": True,
            "scope": spec.scope,
            "reason": f"工具 {spec.name} 属敏感操作，调用即进审批，账不动",
        }
    return {
        "allowed": True,
        "approval_required": False,
        "scope": spec.scope,
        "reason": "",
    }


def ensure_allowed(*, roles: list[str], spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """策略不通过直接抛 4006（调用方无需自己拼错误信封）。"""
    decision = check(roles=roles, spec=spec, args=args)
    if not decision["allowed"]:
        raise BusinessError(
            ErrorCode.TOOL_SCOPE_DENIED,
            f"{decision['reason']}（可联系管理员开通 {decision['scope']}）",
            403,
        )
    return decision
