"""v1 路由聚合（唯一注册处：新增端点只改这里）

链路：app.py include_router(api_router, prefix="/api/v1") → 各端点模块。
"""

from fastapi import APIRouter

from office_agent_server.api import approvals, auth, governance, notifications, tasks, tools

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(tools.router)
api_router.include_router(tasks.router)
api_router.include_router(approvals.router)
api_router.include_router(notifications.router)
api_router.include_router(governance.router)
