"""runtime 挂载扩展位（server 不认识 runtime；装了才启用）

链路：__main__ 侧 create_app() → mount(app) → include_router(prefix="/api/v1")
      → /api/v1/agents 与 /api/v1/runs 生效。

时序红线：mount 必须发生在宿主 startup（lifespan / init_models）**之前**——
RunStep 挂在 server 的共享 Base metadata 上，import api 即完成注册，
随壳层建表一起建出来；晚于建表才挂载会导致 run_steps 缺表。
不装 runtime = 纯地基模式，server 行为零变化。
"""

from __future__ import annotations

from fastapi import FastAPI


def mount(app: FastAPI) -> None:
    """把运行时端点挂到宿主应用（勿重复调用；重复 include 会注册两份路由）。"""
    from office_agent_runtime.api import router

    app.include_router(router, prefix="/api/v1")
