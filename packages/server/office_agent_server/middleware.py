"""trace 中间件（trace 贯穿 + 耗时记录）

链路：请求 → 入站 ``X-Trace-Id`` 沿用（缺失才新生成）→ ContextVar 供端点取用
      → 响应头回写 ``X-Trace-Id`` / ``X-Elapsed-Ms`` → JSON 信封体内补 ``trace_id``。

为什么入站要沿用：跨系统联动时上游 trace 必须与本内核 trace 是同一条线，
否则「同一 trace 在两侧审计里对得上」无从验证。

纯 ASGI 实现：JSON 信封体内补 trace_id；SSE/流式透传不缓冲。
不用 BaseHTTPMiddleware：新版 Starlette 的 call_next 不再返回端点 Response 对象
（body 取不到），逐帧处理与版本无关。
"""

from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def current_trace_id() -> str:
    """本次请求的 trace（端点取用唯一入口）；中间件之外调用返回空串。"""
    return _trace_id.get()


def _elapsed_ms(started: float) -> str:
    """耗时毫秒（X-Elapsed-Ms 响应头）。"""
    return str(int((time.perf_counter() - started) * 1000))


class TraceMiddleware:
    """注入 X-Trace-Id；JSON 信封体内补 trace_id（SSE/流式透传不缓冲）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id = Headers(scope=scope).get("x-trace-id", uuid.uuid4().hex[:16])
        # 与端点同一 task 内的 ContextVar：端点据此取 trace，无需从请求体传
        _trace_id.set(trace_id)
        sender = _TraceSender(send, trace_id, time.perf_counter())
        await self.app(scope, receive, sender)


class _TraceSender:
    """逐帧转发：JSON 缓冲改写，非 JSON（SSE 等）原样透传。"""

    def __init__(self, send: Send, trace_id: str, started: float) -> None:
        self._send = send
        self._trace_id = trace_id
        self._started = started
        self._status = 200
        self._headers = MutableHeaders(raw=[])
        self._is_json: bool | None = None
        self._chunks: list[bytes] = []

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            await self._on_start(message)
            return
        if message["type"] == "http.response.body" and self._is_json:
            await self._on_body(message)
            return
        await self._send(message)

    async def _on_start(self, message: Message) -> None:
        """记录状态与头；非 JSON 立刻透发 start（不缓冲流），JSON 等收齐 body 再发。"""
        self._status = int(message.get("status", 200))
        raw = message.get("headers", [])
        pairs: list[tuple[bytes, bytes]] = [(k, v) for k, v in raw if isinstance(k, bytes)]
        self._headers = MutableHeaders(raw=pairs)
        self._headers["X-Trace-Id"] = self._trace_id
        content_type = self._headers.get("content-type", "").split(";")[0].strip()
        self._is_json = content_type == "application/json"
        if not self._is_json:
            await self._flush_start()

    async def _flush_start(self) -> None:
        """发出缓存的 start 帧（JSON 改写完或透传时调用）。"""
        self._headers["X-Elapsed-Ms"] = _elapsed_ms(self._started)
        await self._send(
            {"type": "http.response.start", "status": self._status, "headers": self._headers.raw}
        )

    async def _on_body(self, message: Message) -> None:
        """JSON 体收齐后注入 trace_id 再发出；解析失败原样透传。"""
        body = message.get("body", b"")
        if isinstance(body, (bytes, bytearray)):
            self._chunks.append(bytes(body))
        if message.get("more_body", False):
            return
        raw = b"".join(self._chunks)
        try:
            payload: Any = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            payload.setdefault("trace_id", self._trace_id)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers["Content-Length"] = str(len(raw))
        await self._flush_start()
        await self._send({"type": "http.response.body", "body": raw, "more_body": False})
