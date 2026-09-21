"""工具注册中心（领域无关）：进程内 ToolSpec 注册表。

职责：register() 注册（重名抛 ToolError 拒绝，防覆盖劫持）；get() 按名取（不存在 404）；list_all() 全量列表（按名排序）。
链路：启动期 tools_office / tools_ecommerce 调 register()；executor / approvals / 路由经 get() 与 list_all() 消费。
对齐：docs/office-agent仓库骨架与内核提取方案.md §3（registry.py 原样迁）。
"""

from office_agent.contracts import ToolError, ToolSpec

_specs: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    """注册一个工具；重名注册直接拒绝（宁可启动失败也不静默覆盖）。"""
    if spec.name in _specs:
        raise ToolError(409, f"工具重名注册被拒绝：{spec.name} 已存在，请更换工具名")
    _specs[spec.name] = spec


def get(name: str) -> ToolSpec:
    """按名取工具规格；不存在抛 404。"""
    spec = _specs.get(name)
    if spec is None:
        raise ToolError(404, f"工具不存在：{name}，请先调 GET /tools 查看可用工具列表")
    return spec


def list_all() -> list[ToolSpec]:
    """全量工具列表（按名称排序，保证输出稳定）。"""
    return sorted(_specs.values(), key=lambda s: s.name)
