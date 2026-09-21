"""FastAPI 应用壳：lifespan 建库+注册工具、全部 HTTP 路由（统一信封）、CORS、可选前端静态托管。

职责：
- lifespan：init_db 建三表 → tools_office.register_all() 注册内置办公工具 → tools_ecommerce.register_if_enabled()
  条件注册外部系统桥接工具（可选插件，不在线只 warning 不阻塞启动）；
- 路由：/healthz、/auth/login 免鉴权；/tools、/tools/invoke、/tasks、/approvals、/approvals/{id}/decide 全挂 get_current；
- 统一信封：成功 {ok:true,data}；业务失败（ToolError）{ok:false,error:{code,message}}；鉴权失败 HTTP 401；
- apps/web/dist 存在则挂 StaticFiles(html=True)，不存在跳过（不强依赖前端构建产物）；CORS 全开（开发期）。
链路：请求 → get_current 鉴权 → 薄路由（解析 → executor / approvals）→ ok/fail 信封。
对齐：docs/office-agent仓库骨架与内核提取方案.md §2（server 壳 auth/tasks/approvals/audit）、§3（bootstrap 改 app 工厂、去种子依赖）。
"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent import (
    approvals,
    auth,
    executor,
    registry,
    tools_ecommerce,
    tools_office,
)
from office_agent.contracts import ToolError
from office_agent.db import Approval, Task, get_db, init_db, list_tasks

logger = logging.getLogger("office_agent.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动期：建表 + 注册工具（内置必注册，桥接插件可选）。"""
    await init_db()
    tools_office.register_all()
    await tools_ecommerce.register_if_enabled()
    names = "、".join(s.name for s in registry.list_all())
    logger.info("office-agent 启动完成，已注册 %d 个工具：%s", len(registry.list_all()), names)
    yield


app = FastAPI(
    title="office-agent",
    version="0.1.0",
    description="领域无关的开源智能办公 Agent 骨架：工具注册 / Scope 鉴权 / 审批闸门 / 全链路审计",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str
    password: str


class InvokeRequest(BaseModel):
    """工具调用请求体。"""

    name: str
    args: dict = {}


class DecideRequest(BaseModel):
    """审批决定请求体。"""

    approve: bool
    comment: str = ""


def _fail(exc: ToolError) -> dict:
    """ToolError → 统一失败信封。"""
    return {"ok": False, "error": {"code": exc.code, "message": exc.message}}


def _iso(dt) -> str | None:
    """datetime → ISO 字符串（UTC，补 Z 后缀）；空值返回 None。"""
    return dt.isoformat() + "Z" if dt else None


def _task_row(task: Task) -> dict:
    return {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "progress": task.progress,
        "result": json.loads(task.result_json) if task.result_json else None,
        "error": task.error,
        "created_by": task.created_by,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
    }


def _approval_row(approval: Approval) -> dict:
    return {
        "id": approval.id,
        "tool_name": approval.tool_name,
        "args": json.loads(approval.args_json) if approval.args_json else {},
        "reason": approval.reason,
        "status": approval.status,
        "requested_by": approval.requested_by,
        "decided_by": approval.decided_by,
        "comment": approval.comment,
        "created_at": _iso(approval.created_at),
        "decided_at": _iso(approval.decided_at),
    }


@app.get("/healthz")
async def healthz():
    """健康检查（免鉴权）：含当前已注册工具数。"""
    return {"status": "ok", "version": app.version, "tool_count": len(registry.list_all())}


@app.post("/auth/login")
async def login(body: LoginRequest):
    """登录（免鉴权）：校验种子口令，签发 JWT。"""
    user = auth.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = auth.create_access_token(user)
    return {
        "ok": True,
        "data": {
            "token": token,  # 契约字段（前端 api.ts 取用）
            "access_token": token,  # OAuth2 风格别名，二者等值
            "token_type": "bearer",
            "username": user.username,
            "scopes": user.scopes,
        },
    }


@app.get("/tools")
async def list_tools(user: auth.CurrentUser = Depends(auth.get_current)):
    """当前用户 scope 可见的工具列表（含 JSON Schema，便于前端渲染调用表单）。"""
    visible = [s for s in registry.list_all() if s.scope in user.scopes]
    return {
        "ok": True,
        "data": [
            {
                "name": s.name,
                "description": s.description,
                "scope": s.scope,
                "needs_approval": s.needs_approval,
                "schema": s.schema,
            }
            for s in visible
        ],
    }


@app.post("/tools/invoke")
async def invoke_tool(
    body: InvokeRequest,
    user: auth.CurrentUser = Depends(auth.get_current),
    session: AsyncSession = Depends(get_db),
):
    """调用工具：{name,args} → executor（Scope 校验/审批分流/超时/审计）→ 统一信封。"""
    try:
        data = await executor.invoke(
            session, actor=user.username, scopes=user.scopes, name=body.name, args=body.args
        )
        return {"ok": True, "data": data}
    except ToolError as exc:
        return _fail(exc)


@app.get("/tasks")
async def list_task_rows(
    user: auth.CurrentUser = Depends(auth.get_current), session: AsyncSession = Depends(get_db)
):
    """任务执行记录（最新在前）。"""
    rows = await list_tasks(session)
    return {"ok": True, "data": [_task_row(t) for t in rows]}


@app.get("/approvals")
async def list_approval_rows(
    user: auth.CurrentUser = Depends(auth.get_current), session: AsyncSession = Depends(get_db)
):
    """审批单列表（最新在前）。"""
    rows = await approvals.list_all(session)
    return {"ok": True, "data": [_approval_row(a) for a in rows]}


@app.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: int,
    body: DecideRequest,
    user: auth.CurrentUser = Depends(auth.get_current),
    session: AsyncSession = Depends(get_db),
):
    """审批决定：approve=true 执行原工具并落任务；false 驳回。审批人≠提交人（同人 1001）。"""
    try:
        data = await approvals.decide(
            session,
            approval_id=approval_id,
            approver=user.username,
            approve=body.approve,
            comment=body.comment,
        )
        return {"ok": True, "data": data}
    except ToolError as exc:
        return _fail(exc)


_WEB_DIST = Path(__file__).resolve().parent.parent / "apps" / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")
else:
    logger.info("未发现前端构建产物（apps/web/dist），跳过静态托管，仅提供 API 服务")
