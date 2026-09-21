"""上游提供方注册表（provider_id → 客户端）

链路：宿主启动 → configure_from_settings() 按 Settings.LINKAGE_PROVIDERS 建好客户端
      → executor 执行远程工具时 provider_of(provider_id) 取用。

口径：
- 凭据只从 Settings（环境变量）来，本模块不接受来自工具声明或请求体的地址/令牌；
- 未配置即报错（503 + 中文可操作提示），**绝不静默降级成假数据**；
- configured() 供插件自检：provider 没配就不注册对应工具，避免注册一个调不通的工具。
"""

from __future__ import annotations

import threading

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.linkage.provider import RemoteToolProvider
from office_agent_core.settings import ProviderConfig, settings

_lock = threading.RLock()
_providers: dict[str, RemoteToolProvider] = {}


def register_provider(provider: RemoteToolProvider, *, replace: bool = True) -> RemoteToolProvider:
    """登记提供方（同名默认覆盖，便于测试与热更新）。"""
    with _lock:
        if provider.provider_id in _providers and not replace:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"上游提供方已注册：{provider.provider_id}"
            )
        _providers[provider.provider_id] = provider
    return provider


def get_provider(provider_id: str) -> RemoteToolProvider | None:
    """软取（不抛错），供自检与巡检用。"""
    return _providers.get((provider_id or "").strip())


def configured(provider_id: str) -> bool:
    """该提供方是否已配置（插件据此决定是否注册远程工具）。"""
    return get_provider(provider_id) is not None


def provider_of(provider_id: str) -> RemoteToolProvider:
    """取提供方；未配置抛 5001（503），提示可直接照做的配置方式。"""
    provider = get_provider(provider_id)
    if provider is None:
        raise BusinessError(
            ErrorCode.UPSTREAM_FAILED,
            f"上游提供方 {provider_id or '（空）'} 未配置，无法调用其工具；"
            f"请在环境变量 LINKAGE_PROVIDERS 中补上该提供方的 base_url 与 token",
            503,
        )
    return provider


def configure_from_settings(providers: dict[str, ProviderConfig] | None = None) -> list[str]:
    """按 Settings 建好全部提供方客户端，返回已配置的 provider_id 列表。"""
    source = settings.LINKAGE_PROVIDERS if providers is None else providers
    ids: list[str] = []
    for provider_id, config in source.items():
        register_provider(RemoteToolProvider.from_config(provider_id, config))
        ids.append(provider_id)
    return sorted(ids)


async def aclose_all() -> None:
    """关闭全部自有客户端（宿主关停时调用）。"""
    with _lock:
        providers = list(_providers.values())
    for provider in providers:
        await provider.aclose()


def shutdown_providers() -> None:
    """同步清空注册表（仅测试用；不负责关闭客户端）。"""
    with _lock:
        _providers.clear()


def reset() -> None:
    """清空注册表（别名，语义同 shutdown_providers）。"""
    shutdown_providers()


def configured_ids() -> list[str]:
    """已配置的 provider_id（巡检输出用）。"""
    with _lock:
        return sorted(_providers)