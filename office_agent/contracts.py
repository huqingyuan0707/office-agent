"""内核契约层：ToolSpec 工具规格 + ToolError 统一错误 + Scope 常量（领域无关）。

职责：
- ToolSpec：一个工具的完整静态描述六要素（名称/描述/scope/是否需审批/JSON Schema/async handler），是注册中心的最小注册单元；
- ToolError：内核全链路统一业务错误（数字 code + 中文可操作 message），路由层转 {ok:false,error:{code,message}} 信封；
- SCOPE_READ / SCOPE_WRITE：授权范围常量（office:read / office:write），auth / registry / executor 共用同一口径。
链路：tools_office / tools_ecommerce 构造 ToolSpec → registry.register → executor 按 spec 执行；任一环节失败抛 ToolError。
对齐：docs/office-agent仓库骨架与内核提取方案.md §3（contracts.py 原样迁）、§4（Scope 列）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"


class ToolError(Exception):
    """内核统一业务错误：code 为数字错误码，message 为中文可操作提示。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolSpec:
    """工具规格（领域无关：任何业务域的工具都可描述为这六要素）。"""

    name: str
    description: str
    scope: str
    needs_approval: bool
    schema: dict
    handler: Callable[[dict], Awaitable[dict]]
