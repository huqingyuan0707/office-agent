"""工具执行器（领域无关）：Scope 硬拦 → 审批分流 → 超时执行 → 全程审计。

职责：
- invoke()：唯一执行入口。工具不存在 → 404；缺 scope → 403 硬拦；needs_approval=True 不执行、
  转 approvals 建单返回 pending；只读工具经 execute_tool 带超时执行，成功/失败均落 tasks + audit_logs；
- execute_tool()：带 asyncio.wait_for 超时的纯执行底座，invoke 与审批批准后执行（approvals.decide）共用；
- trace_id：uuid4 短码（8 位），贯穿一次调用的审计与任务记录。
链路：POST /tools/invoke → invoke() → registry 取 spec → scope 校验 →（审批分流 | 超时执行）→ tasks / audit_logs。
对齐：docs/office-agent仓库骨架与内核提取方案.md §3（executor.py 超时/全分支审计原样迁）、ADR-0003 §3（恒送审口径）。
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from office_agent import registry
from office_agent.config import settings
from office_agent.contracts import ToolError, ToolSpec
from office_agent.db import record_audit, record_task


def new_trace_id() -> str:
    """生成短码 trace_id（uuid4 前 8 位）。"""
    return uuid.uuid4().hex[:8]


async def execute_tool(spec: ToolSpec, args: dict, trace_id: str = "") -> dict:
    """带超时的纯执行底座（不含鉴权/审批/审计）：handler 只拿 args，返回 dict。

    handler 内抛出的 ToolError 原样透传（保留业务错误码）；其余异常统一包成 500；
    超时硬拦为 504，绝不挂死请求。
    """
    try:
        return await asyncio.wait_for(spec.handler(dict(args)), timeout=float(settings.TOOL_TIMEOUT_SECONDS))
    except asyncio.TimeoutError:
        raise ToolError(504, f"工具 {spec.name} 执行超时（上限 {settings.TOOL_TIMEOUT_SECONDS} 秒），可稍后重试或调大 TOOL_TIMEOUT_SECONDS")
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 工具侧任意异常都不得裸抛到路由层
        raise ToolError(500, f"工具 {spec.name} 执行失败：{exc}")


async def invoke(session: AsyncSession, *, actor: str, scopes: list[str], name: str, args: dict) -> dict:
    """执行入口：Scope 硬拦 + 审批分流 + 超时执行 + 全程审计。

    返回 dict：
    - 需审批工具 → {"status":"pending","approval_id":N,"message":中文说明}（本次不执行）；
    - 只读工具   → {"status":"succeeded","trace_id":...,"data":handler结果}。
    """
    spec = registry.get(name)
    if spec.scope not in scopes:
        await record_audit(session, actor=actor, action="tool.invoke", detail={"tool": name, "args": args, "outcome": "scope_denied", "need_scope": spec.scope})
        raise ToolError(403, f"无权限调用工具 {name}：当前令牌缺少授权范围 {spec.scope}")

    if spec.needs_approval:
        # 延迟导入避免 executor ↔ approvals 循环依赖
        from office_agent import approvals

        approval = await approvals.create(session, tool_name=name, args=args, requested_by=actor)
        await record_audit(session, actor=actor, action="tool.invoke", detail={"tool": name, "args": args, "outcome": "approval_pending", "approval_id": approval.id})
        return {
            "status": "pending",
            "approval_id": approval.id,
            "message": f"工具 {name} 需审批，本次调用未执行；已创建审批单 #{approval.id}，请由其他用户审批",
        }

    trace_id = new_trace_id()
    await record_audit(session, actor=actor, action="tool.invoke.start", detail={"tool": name, "args": args}, trace_id=trace_id)
    try:
        result = await execute_tool(spec, args, trace_id)
    except ToolError as exc:
        await record_task(session, type_=name, status="failed", created_by=actor, error=exc.message)
        await record_audit(session, actor=actor, action="tool.invoke.end", detail={"tool": name, "outcome": "failed", "code": exc.code, "message": exc.message}, trace_id=trace_id)
        raise
    await record_task(session, type_=name, status="succeeded", created_by=actor, result=result)
    await record_audit(session, actor=actor, action="tool.invoke.end", detail={"tool": name, "outcome": "succeeded"}, trace_id=trace_id)
    return {"status": "succeeded", "trace_id": trace_id, "data": result}
