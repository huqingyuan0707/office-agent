"""跨系统联动层（内核唯一的出站出口）

红线（与 docs/office-agent仓库骨架与内核提取方案.md §7 一致）：
- 主包永不直连任何上游数据库；所有跨系统数据一律经**工具调用**取得；
- 拉取结果的溯源（source_endpoint / fetched_at）由本层**实测填写**，不编造、不透传；
- 凭据只从环境变量（Settings.LINKAGE_PROVIDERS）读，绝不入库、绝不写进 ToolSpec；
- provider 未配置时**报错而非降级**：宁可失败，也不能拿演示假数据顶替真实上游。
"""

from office_agent_core.linkage.provider import RemoteToolProvider
from office_agent_core.linkage.registry import (
    aclose_all,
    configure_from_settings,
    configured,
    configured_ids,
    get_provider,
    provider_of,
    register_provider,
    reset,
    shutdown_providers,
)

__all__ = [
    "RemoteToolProvider",
    "aclose_all",
    "configure_from_settings",
    "configured",
    "configured_ids",
    "get_provider",
    "provider_of",
    "register_provider",
    "reset",
    "shutdown_providers",
]
