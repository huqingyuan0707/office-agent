"""FastAPI 应用工厂（启动装配 + 中间件 + 异常统一收口）

链路：create_app() → 中间件 → /api/v1 router → /health|/ready 探针。
异常统一收口信封：BusinessError 按号段码；HTTP 401/403/404 转 1002/1003/1004；
参数校验转 1001；未知异常转 5000（不泄露堆栈）。

启动装配（lifespan，顺序即依赖顺序）：
1. 建表（幂等，防空库 500）；
2. 注入审计 sink（内核只发事件，落库实现由壳提供）；
3. 种子账号（admin + reviewer，SEED_ON_START=false 可关，生产必关）；
4. 按 Settings 建上游提供方客户端（凭据只来自环境变量）；
5. 装载内置办公工具包（office-agent-tools-office pip 包）；
6. 装载 plugins/ 下的工具插件（每个插件自行决定「提供方没配就不注册」）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from office_agent_core import linkage
from office_agent_core.audit import set_sink
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import is_default_seed_password, settings
from office_agent_server.api import api_router
from office_agent_server.audit_sink import sql_audit_sink
from office_agent_server.db import init_models
from office_agent_server.middleware import TraceMiddleware
from office_agent_server.plugins import load_plugin_tools
from office_agent_server.responses import fail
from office_agent_server.seed import seed_on_startup

try:
    # tools-office 是可选 pip 包；未安装不阻断启动
    from office_agent_tools_office import register_all as _office_register

    _OFFICE_TOOLS_AVAILABLE = True
except ImportError:
    _OFFICE_TOOLS_AVAILABLE = False

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动装配 + 关停释放（关停只关自有 HTTP 客户端，不动宿主资源）。"""
    await init_models()
    set_sink(sql_audit_sink)
    if settings.SEED_ON_START:
        if is_default_seed_password(settings.SEED_PASSWORD.get_secret_value()):
            logger.warning(
                "种子账号仍用本地默认口令，仅限本机演示，禁止暴露到公网；"
                "生产用 ENV=prod 关种子并改用显式口令"
            )
        await seed_on_startup()
    providers = linkage.configure_from_settings()

    # 内置办公工具包（pip 包；未装则跳过，不阻断启动）
    office_names: list[str] = []
    if _OFFICE_TOOLS_AVAILABLE:
        try:
            office_names = _office_register()
            logger.info("已装载内置办公工具包：%s", "、".join(office_names))
        except Exception as exc:
            logger.warning("内置办公工具包装载失败：%s", str(exc)[:200])

    loaded = load_plugin_tools()
    logger.info(
        "上游提供方已配置：%s；已装载插件：%s",
        "、".join(providers) or "（无）",
        "、".join(loaded) or "（无）",
    )
    yield
    await linkage.aclose_all()


def create_app() -> FastAPI:
    """应用工厂（测试可反复调用；进程内单例由 __main__ 侧持有）。"""
    app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api/v1")
    _register_exception_handlers(app)
    return app


def _http_to_envelope(status: int, detail: object) -> JSONResponse:
    """HTTP 异常转信封：401→1002/403→1003/404→1004，其余→5000（前端按码分支）。"""
    text = str(detail) if detail else ""
    if status == 401:
        return fail(ErrorCode.UNAUTHORIZED, text or "未登录或登录已过期", 401)
    if status == 403:
        return fail(ErrorCode.FORBIDDEN, text or "权限不足", 403)
    if status == 404:
        return fail(ErrorCode.NOT_FOUND, text or "资源不存在", 404)
    return fail(ErrorCode.INTERNAL, "系统繁忙，请稍后重试", status if status < 500 else 500)


def _register_exception_handlers(app: FastAPI) -> None:
    """统一收口（端点不写 try/except：业务分支留在内核与插件里）。"""

    @app.exception_handler(BusinessError)
    async def business_error_handler(_: Request, exc: BusinessError) -> JSONResponse:
        return fail(exc.code, exc.msg, exc.http_status)

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return _http_to_envelope(int(exc.status_code), exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _http_to_envelope(int(exc.status_code), exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("param invalid: %s", str(exc)[:300])
        return fail(ErrorCode.PARAM_INVALID, "请求参数有误，请检查后重试", 400)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return fail(ErrorCode.INTERNAL, "系统繁忙，请稍后重试", 500)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """存活探针。"""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        """就绪探针（依赖检查后续补）。"""
        return {"status": "ready"}


app = create_app()
