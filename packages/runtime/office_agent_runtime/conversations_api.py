"""多轮对话会话端点（薄封装：解析 → 服务 → ok()，业务全在 conversations.py / runner）

链路：POST /conversations（新建）→ GET /conversations（列表）
      → GET /conversations/{id}（详情+消息）→ POST /conversations/{id}/messages（发消息：
      落用户消息 → 构造最近上下文 → 自动路由 + start_run 发起 run → 落助手消息记 run_id）
      → DELETE /conversations/{id}（连带消息删除）。

口径：
- 隔离/越权统一 1004（服务层同一口径）；trace 取入站 X-Trace-Id 与 /runs 一致；
- 消息发送与 POST /runs 共用受理链（greeting 即时回复 / 自动路由 / start_run），
  context 由服务层从会话历史构造后透传给 start_run（checkpoint 持久化，续跑回放）；
- 助手消息文案 = run 概要的 answer 或错误/状态提示，run_id 关联后前端轮询
  GET /runs/{id} 刷新完整时间线（与 /runs 现有前端口径一致，零重复实现）。
对齐：AGENTS.md §3 分层/薄封装 + PRD §6 多轮上下文理解、主动追问、可审计。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError
from office_agent_runtime import conversations
from office_agent_runtime.router import is_greeting_only, route_agent_spec
from office_agent_runtime.runner import instant_greeting_run, start_run
from office_agent_runtime.spec import compose_context_goal
from office_agent_server.db import get_db
from office_agent_server.middleware import current_trace_id
from office_agent_server.rbac import CurrentUser, get_current_user
from office_agent_server.responses import ok

router = APIRouter(tags=["conversations"])


class CreateConversationRequest(BaseModel):
    """新建会话入参（title 可空，服务层给默认「新对话」）。"""

    title: str = Field(default="", max_length=120)


class SendMessageRequest(BaseModel):
    """会话发消息入参（text 即一句话目标，与 POST /runs 的 goal 同口径）。"""

    text: str = Field(min_length=1, max_length=500)


def _agent_reply(summary: dict[str, Any]) -> str:
    """助手消息文案：answer 优先 → 审批挂起提示 → 错误/状态兜底（如实，不编造）。"""
    answer = str(summary.get("answer") or "").strip()
    if answer:
        return answer
    pending = summary.get("pending_approval")
    if isinstance(pending, dict) and pending.get("approval_id"):
        return "已受理并生成审批申请（写动作恒送审），复核员批准后自动继续，可前往审批页查看。"
    error = str(summary.get("error") or "").strip()
    if error:
        return f"办理未完成：{error}"
    label = str(summary.get("status_label") or summary.get("status") or "进行中")
    return f"已受理（{label}），正在执行中，可稍后刷新查看结果。"


@router.get("/conversations")
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """当前用户的会话列表（按最近活跃倒序）。"""
    items = await conversations.list_conversations(db, tenant=user.tenant, username=user.username)
    return ok({"total": len(items), "items": items}, "获取成功")


@router.post("/conversations")
async def create_conversation(
    payload: CreateConversationRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """新建会话（对话工作台入口；title 空给默认）。"""
    item = await conversations.create_conversation(
        db, tenant=user.tenant, username=user.username, title=payload.title
    )
    await db.commit()
    return ok(item, "已创建会话")


@router.get("/conversations/{conversation_id}")
async def conversation_detail(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """会话详情（含全部消息；越权/不存在 1004）。"""
    detail = await conversations.get_conversation(
        db, tenant=user.tenant, username=user.username, conversation_id=conversation_id
    )
    return ok(detail, "获取成功")


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """删除会话（连带消息；仅当前用户自己的会话）。"""
    await conversations.delete_conversation(
        db, tenant=user.tenant, username=user.username, conversation_id=conversation_id
    )
    await db.commit()
    return ok({"deleted": conversation_id}, "已删除会话")


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """会话内发消息：落用户消息 → 带最近上下文发起 run → 落助手消息。

    返回 {user_message, agent_message, run}——run 与 POST /runs 同形状，
    前端照常轮询 GET /runs/{id} 刷新时间线；纯寒暄走即时回复（零工具）。
    """
    text = payload.text.strip()
    await conversations.append_message(
        db,
        tenant=user.tenant,
        username=user.username,
        conversation_id=conversation_id,
        role="user",
        content=text,
    )
    # 多轮上下文：最近 N 条历史消息拼摘要，随新目标一起进规划（PRD §6 指代/补充理解）
    context = await conversations.recent_context(
        db, tenant=user.tenant, conversation_id=conversation_id
    )
    try:
        if is_greeting_only(text):
            summary = await instant_greeting_run(db, goal=text, user=user)
        else:
            # 路由也吃「上下文 + 新目标」拼接（与规划同一口径）：「再加一个明日计划」
            # 能回到「生成工作日报」的智能体上，而不是裸词掉进 LLM 兜底
            spec = route_agent_spec(compose_context_goal(context, text))
            summary = await start_run(
                db,
                spec=spec,
                goal=text,
                user=user,
                trace_id=current_trace_id(),
                context=context,
            )
    except BusinessError as exc:
        # 路由/规划失败不落 run：助手消息如实记录失败原因（可审计、可追问），仍返回 code 0
        msg = await conversations.append_message(
            db,
            tenant=user.tenant,
            username=user.username,
            conversation_id=conversation_id,
            role="agent",
            content=exc.msg,
        )
        await db.commit()
        return ok(
            {"user_message": text, "agent_message": msg.id, "run": None, "error": exc.msg},
            "已记录回复",
        )
    reply = _agent_reply(summary)
    run_id = str(summary.get("run_id") or "")
    agent_msg = await conversations.append_message(
        db,
        tenant=user.tenant,
        username=user.username,
        conversation_id=conversation_id,
        role="agent",
        content=reply,
        run_id=run_id,
    )
    await db.commit()
    return ok(
        {
            "user_message": text,
            "agent_message": agent_msg.id,
            "run": summary,
            "error": "",
        },
        "已受理并回复",
    )
