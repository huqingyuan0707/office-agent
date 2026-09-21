"""业务服务层（目录包）。

职责：纯函数，不依赖 FastAPI 对象（Depends 例外），入参显式、出参显式；
      端点薄封装，业务逻辑全部沉到 services/。
对齐：AGENTS.md §3（分层红线：端点薄封装，业务进 services/ 纯函数）。
"""

from .approval_flow import create_approval, decide_approval

__all__ = ["create_approval", "decide_approval"]
