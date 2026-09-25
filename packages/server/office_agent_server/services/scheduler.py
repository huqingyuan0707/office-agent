"""定时调度环（asyncio 后台任务：PRD §2.11 内置定时器，替代外部 cron 调 scan 端点）。

职责：start/stop 生命周期 + _loop() 每 SCHEDULER_TICK_SECONDS 秒起独立会话跑
      tick_due_jobs()——到点判定/执行/推进全在 services/jobs。
口径：Settings.SCHEDULER_ENABLED 默认 False（测试与按需部署不起环，.env.example
      文档化开法）；每轮异常一律 catch 记告警后继续——调度环绝不因单轮故障而死，
      降级只体现在日志与作业 last_result，绝不 500 影响主服务。
链路：app.py lifespan（enabled 才 start，yield 后 stop）→ 本模块 → jobs.tick_due_jobs。
对齐：AGENTS.md §3（降级不阻断）；智能办公Agent 产品需求文档.md §2.11。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from office_agent_core.settings import settings
from office_agent_server.db import session_factory
from office_agent_server.services.jobs import tick_due_jobs

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _loop() -> None:
    """调度主循环（睡一个 tick → 独立会话扫到点作业 → 无限继续）。"""
    tick = max(5, int(settings.SCHEDULER_TICK_SECONDS))
    logger.info("scheduler loop started (tick=%ds)", tick)
    while True:
        await asyncio.sleep(tick)
        try:
            async with session_factory()() as db:
                await tick_due_jobs(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("scheduler tick failed", exc_info=True)


async def start_scheduler() -> None:
    """启动调度环（幂等：重复调用不产生第二个环）。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="office-agent-scheduler")


async def stop_scheduler() -> None:
    """停止调度环（lifespan 关停用；环不存在时静默）。"""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
    _task = None
