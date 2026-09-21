"""统一响应信封（禁止裸返回）

链路：端点 → ok()/fail() → ``{code,msg,data,trace_id}``（trace_id 由 TraceMiddleware 补进响应体）。
口径与上游联动对端保持一致：``code == 0`` 为成功，非 0 由前端按号段分支。
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from office_agent_core.errors import ErrorCode


def ok(data: Any, msg: str = "操作成功") -> dict[str, Any]:
    """成功信封，code 恒 0；trace_id 由 TraceMiddleware 补进响应体。"""
    return {"code": 0, "msg": msg, "data": data}


def fail(code: ErrorCode | int, msg: str, http_status: int = 400) -> JSONResponse:
    """失败信封，trace_id 由中间件注入。"""
    return JSONResponse(
        status_code=http_status,
        content={"code": int(code), "msg": msg, "data": None},
    )
