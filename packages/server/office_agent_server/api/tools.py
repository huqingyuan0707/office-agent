"""工具注册中心端点（清单 / 详情 / 受控试调 + 送审分流）

链路：GET /agent/tools（清单，含 Scope/Schema/超时重试/熔断态）
      → GET /agent/tools/{name}（单工具详情）
      → POST /agent/tools/{name}/invoke（受控试调：鉴权 → 分流 → 执行/落审批单）。

薄封装红线：本文件只解析入参 + 调内核/services + ok()/fail()；
            鉴权、参数校验、超时重试、熔断、审计全在 office_agent_core 内，端点不复制一份。
送审分流红线：spec.requires_approval=True 的工具 invoke 不落 executor，只落审批单后返回
            pending_approval——写动作的唯一放行口是审批中心。
trace 红线：trace 取入站 ``X-Trace-Id``（中间件沿用），不从请求体取，
            这样跨系统联动时两侧审计里的 trace 是同一条线。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import executor, policy, registry
from office_agent_core.contracts import ToolContext, validate_args
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.db import get_db
from office_agent_server.middleware import current_trace_id
from office_agent_server.rbac import CurrentUser, get_current_user
from office_agent_server.responses import ok
from office_agent_server.services.approval_flow import create_approval

router = APIRouter(prefix="/agent", tags=["agent"])


class InvokeRequest(BaseModel):
    """工具试调入参（端点私有 DTO）。"""

    args: dict[str, Any] = {}
    session_id: str = ""


def _tool_view(name: str) -> dict[str, Any]:
    """工具出参 = 规格 + 当前熔断态（运维与前端同源，避免各查各的）。"""
    spec = registry.get(name)
    return {**registry.spec_to_dict(spec), "breaker": executor.breaker_snapshot(spec.name)}


@router.get("/tools")
async def list_tools(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """已注册工具清单（注册中心为空给中文提示不报错）。"""
    _ = user
    data = registry.list_tools()
    items: list[dict[str, Any]] = [
        {**item, "breaker": executor.breaker_snapshot(item["name"])} for item in data["items"]
    ]
    msg = "获取成功" if items else "暂无已注册工具，请检查插件是否已配置对应的上游提供方"
    return ok({"total": len(items), "items": items}, msg)


@router.get("/tools/{name}")
async def get_tool(name: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """单工具详情（未注册 4005，附可用工具名便于自查）。"""
    _ = user
    return ok(_tool_view(name), "获取成功")


@router.post("/tools/{name}/invoke")
async def invoke_tool(
    name: str,
    payload: InvokeRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """受控试调：鉴权 → 参数校验 → 分流（送审 / 直执行）。

    分流规则：spec.requires_approval=True → 落审批单，返回 pending_approval，不调 executor；
             否则 → executor.call 完整执行链（熔断、重试、审计）。
    """
    trace = current_trace_id()
    spec = registry.get(name)

    # --- 策略鉴权 + 参数校验（分流前统一校验，避免「先过了审批再被拒」的口子） ---
    policy.ensure_allowed(roles=list(user.roles), spec=spec, args=payload.args)
    errors = validate_args(spec.params, payload.args)
    if errors:
        raise BusinessError(ErrorCode.PARAM_INVALID, "；".join(errors))

    # --- 送审分流：需审批工具只落单，不调 executor ---
    if spec.requires_approval:
        row = await create_approval(
            db,
            tenant=user.tenant,
            tool_name=spec.name,
            args=payload.args,
            applicant=user.username,
            session_id=payload.session_id,
        )
        data = {
            "status": "pending_approval",
            "approval_required": True,
            "approval_id": row.id,
            "tool": spec.name,
            "scope": spec.scope,
            "trace_id": trace,
        }
        return ok(data, "已提交审批，待人工确认后生效")

    # --- 直执行路径（原 executor.call 完整链） ---
    ctx = ToolContext(
        db=db,
        tenant=user.tenant,
        username=user.username,
        roles=list(user.roles),
        session_id=payload.session_id,
        trace_id=trace,
    )
    try:
        data = await executor.call(ctx, name=name, args=payload.args, trace_id=trace)
    finally:
        # 成败都提交：内核好坏两条路径都发审计，失败一次就丢一条留痕等于审计断链
        # （失败信封本身由统一异常处理器收口，这里不做任何业务分支）
        await db.commit()
    return ok(data, "调用成功")
