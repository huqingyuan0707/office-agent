"""office_agent 包入口：声明元信息并重导出内核公开 API。

职责：外部使用方 `from office_agent import ToolSpec, ToolError, register` 即可定义/注册工具，无需感知内部模块划分。
链路：本包不引入 executor/approvals/db 等重模块，保持「只依赖契约层」的轻入口。
对齐：docs/office-agent仓库骨架与内核提取方案.md §3（__init__.py 公开 API 重导出行）。
"""

from office_agent.contracts import SCOPE_READ, SCOPE_WRITE, ToolError, ToolSpec
from office_agent.registry import get, list_all, register

__all__ = ["SCOPE_READ", "SCOPE_WRITE", "ToolError", "ToolSpec", "get", "list_all", "register"]
__version__ = "0.1.0"
