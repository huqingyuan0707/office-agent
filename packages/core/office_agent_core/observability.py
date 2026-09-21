"""可观测记录（关键链路留痕 → 内存计数器 + JSONL 落盘）

链路：工具调用/模型调用 → record(event, fields) → ①内存计数器与滑窗（即时聚合查询）
      ②后台守护线程批量落 JSONL（重启不丢历史；接 Prometheus/Langfuse 时只改本模块）。

红线：record() 绝不抛错、绝不阻塞主流程（入队即返回，落盘失败只丢该条事件）；
     开关与目录全进 Settings。
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.settings import settings

_lock = threading.Lock()
_counters: dict[str, int] = {}
_flags: dict[str, int] = {}
_recent: deque[dict[str, Any]] = deque(maxlen=500)

_queue: queue.Queue[Any] = queue.Queue(maxsize=2000)
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()


def _ensure_writer() -> None:
    """惰性起守护线程（双检锁）；OBSERVABILITY_ENABLED=false 时不起，只留内存态。"""
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    with _writer_lock:
        if _writer is not None and _writer.is_alive():
            return
        _writer = threading.Thread(target=_writer_loop, name="observability-writer", daemon=True)
        _writer.start()


def _writer_loop() -> None:
    """攒批写盘：最多 50 条或 1 秒一批；队列满即丢（record 侧已保证不阻塞）。

    哨兵 ("__flush__", Event) 让 flush() 能同步等到「此前入队的事件全部落盘」。
    """
    while True:
        batch: list[tuple[float, str, dict[str, Any]]] = []
        deadline = time.time() + 1.0
        while len(batch) < 50 and time.time() < deadline:
            try:
                item = _queue.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if isinstance(item, tuple) and item and item[0] == "__flush__":
                _drain_and_write(batch)
                batch = []
                item[1].set()
                deadline = time.time() + 1.0
                continue
            batch.append(item)
        _drain_and_write(batch)


def _drain_and_write(batch: list[tuple[float, str, dict[str, Any]]]) -> None:
    """落盘一批事件；任何 IO 异常都只丢该批，不外抛。"""
    if not batch:
        return
    try:
        day = datetime.fromtimestamp(batch[0][0], tz=UTC).strftime("%Y%m%d")
        target = Path(settings.OBSERVABILITY_DIR) / f"events-{day}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {"ts": round(ts, 3), "event": event, **fields}, ensure_ascii=False, default=str
            )
            for ts, event, fields in batch
        ]
        with target.open("a", encoding="utf-8") as fp:
            fp.write("\n".join(lines) + "\n")
    except OSError:
        return


def flush(timeout: float = 3.0) -> bool:
    """排空队列并等待写盘完成（测试/退出用；生产路径不依赖）。返回是否按时排空。"""
    if _writer is None or not _writer.is_alive():
        return False
    done = threading.Event()
    _queue.put(("__flush__", done))
    return done.wait(timeout)


def record(event: str, fields: dict[str, Any] | None = None) -> None:
    """记一条关键链路事件：内存计数即时更新，落盘入队异步完成；任何异常都不外抛。"""
    data = dict(fields or {})
    now = time.time()
    try:
        with _lock:
            _counters[event] = _counters.get(event, 0) + 1
            for key, value in data.items():
                if isinstance(value, bool) and value:
                    flag = f"{event}.{key}"
                    _flags[flag] = _flags.get(flag, 0) + 1
            _recent.append({"ts": now, "event": event, **data})
        if settings.OBSERVABILITY_ENABLED:
            _ensure_writer()
            with contextlib.suppress(queue.Full):
                _queue.put_nowait((now, event, data))
    except Exception:
        return


def reset() -> None:
    """清空内存态（仅测试用）。"""
    with _lock:
        _counters.clear()
        _flags.clear()
        _recent.clear()


def snapshot() -> dict[str, Any]:
    """当前内存态的规整聚合（成功率为 None 表示样本不足，不用 0 假装有数据）。"""
    with _lock:
        llm_total = _counters.get("llm", 0)
        tool_total = _counters.get("agent.tool", 0)
        link_total = _counters.get("agent.linkage", 0)
        return {
            "enabled": bool(settings.OBSERVABILITY_ENABLED),
            "dir": settings.OBSERVABILITY_DIR,
            "counters": dict(_counters),
            "flags": dict(_flags),
            "recent": list(_recent)[-20:],
            "llm_ok_rate": round(_flags.get("llm.ok", 0) / llm_total, 4) if llm_total else None,
            "tool_ok_rate": (
                round(_flags.get("agent.tool.ok", 0) / tool_total, 4) if tool_total else None
            ),
            "linkage_ok_rate": (
                round(_flags.get("agent.linkage.ok", 0) / link_total, 4) if link_total else None
            ),
        }
