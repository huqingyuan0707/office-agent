"""工具注册中心（JSON Schema + Scope + 幂等 + 超时/重试 + 熔断策略）

链路：插件 bootstrap 启动注册 → policy.check 取 spec.scope 鉴权
      → executor.call 取 spec 的超时/重试/幂等策略执行。

口径：注册表进程内单例，name 唯一；重复注册视为「热替换同规格」，幂等不报错；
     出参 spec_to_dict 解析出最终生效的超时/重试（None 落 Settings 默认），
     并只投影 remote 的 provider_id/tool（绝不含上游地址与凭据），前端可安全透出。
"""

from __future__ import annotations

import threading
from typing import Any

from office_agent_core.contracts import ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings

_lock = threading.RLock()
_SPECS: dict[str, ToolSpec] = {}


def register(spec: ToolSpec, *, replace: bool = True) -> ToolSpec:
    """注册工具（幂等：同名默认覆盖，便于启动重放与热更新）。"""
    if not spec.name.strip():
        raise BusinessError(ErrorCode.PARAM_INVALID, "工具名不能为空")
    if not spec.scope.strip():
        raise BusinessError(ErrorCode.PARAM_INVALID, f"工具 {spec.name} 必须声明 Scope")
    if spec.is_remote and spec.handler is not None:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"工具 {spec.name} 同时声明了远程绑定与本地实现，二者只能有一个",
        )
    if not spec.is_remote and spec.handler is None:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"工具 {spec.name} 既没有本地实现也没有远程绑定，无法执行",
        )
    with _lock:
        if spec.name in _SPECS and not replace:
            raise BusinessError(ErrorCode.PARAM_INVALID, f"工具已注册：{spec.name}")
        _SPECS[spec.name] = spec
    return spec


def unregister(name: str) -> bool:
    """注销工具（测试清理与热下线用），返回是否真的删掉了。"""
    with _lock:
        return _SPECS.pop(name, None) is not None


def get(name: str) -> ToolSpec:
    """取工具规格；未注册抛 4005（中文提示已注册清单，便于联调自查）。"""
    spec = _SPECS.get((name or "").strip())
    if spec is None:
        known = "/".join(sorted(_SPECS)) or "（暂无）"
        raise BusinessError(ErrorCode.TOOL_NOT_FOUND, f"工具不存在：{name}，可用：{known}", 404)
    return spec


def maybe_get(name: str) -> ToolSpec | None:
    """软取（不抛错），供规划器判断可用性。"""
    return _SPECS.get((name or "").strip())


def names() -> list[str]:
    """已注册工具名（稳定排序，便于快照断言）。"""
    with _lock:
        return sorted(_SPECS)


def all_specs() -> list[ToolSpec]:
    """全部工具规格（按名排序）。"""
    with _lock:
        return [_SPECS[name] for name in sorted(_SPECS)]


def count() -> int:
    """已注册数量。"""
    with _lock:
        return len(_SPECS)


def reset() -> None:
    """清空注册表（仅测试与热重载用）。"""
    with _lock:
        _SPECS.clear()


def timeout_of(spec: ToolSpec) -> float:
    """生效超时（None 落 Settings 默认，禁止散落 30.0 字面量）。"""
    return float(spec.timeout_seconds or settings.AGENT_TOOL_TIMEOUT_SECONDS)


def retries_of(spec: ToolSpec) -> int:
    """生效重试次数；非幂等工具恒 0 次重试（即只调一次）。"""
    if not spec.idempotent:
        return 0
    return int(
        spec.max_retries if spec.max_retries is not None else settings.AGENT_TOOL_MAX_RETRIES
    )


def spec_to_dict(spec: ToolSpec) -> dict[str, Any]:
    """注册中心出参（不含 handler 与上游地址，前端可安全透出）。"""
    return {
        "name": spec.name,
        "scope": spec.scope,
        "description": spec.description,
        "params": spec.params,
        "idempotent": spec.idempotent,
        "requires_approval": spec.requires_approval,
        "approval_action": spec.approval_action,
        "timeout_seconds": timeout_of(spec),
        "max_retries": retries_of(spec),
        # 远程工具只暴露「由哪个提供方实现」，不暴露地址/凭据
        "provider_id": spec.remote.provider_id if spec.remote else "",
    }


def list_tools() -> dict[str, Any]:
    """工具清单（一次带出全部规格，管理台与工具卡片同源）。"""
    items = [spec_to_dict(spec) for spec in all_specs()]
    return {"total": len(items), "items": items}
