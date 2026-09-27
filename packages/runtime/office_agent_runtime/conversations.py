"""多轮对话会话服务（纯函数：建/查/删会话、落消息、构造多轮上下文）

链路：conversations_api 端点薄封装 → 本模块读写 Conversation / ConversationMessage
      → POST 消息时构造最近上下文 → 复用 runner.start_run / router 自动路由发起 run
      → 用户/助手消息落库（助手消息记 run_id，前端轮询 GET /runs/{id} 刷新详情）。

口径：
- 隔离：全部按 (tenant, username) 过滤，端点只传当前用户，越权等同不存在（1004）；
- 上下文只取当前会话最近 N 条用户/助手消息拼摘要，不跨会话、不带第三方内容；
- 消息落库即审计留痕，run_id 关联 Task 行可追溯；
- 只写本模块的表，run 创建走 runner 既有链（受理与执行分离口径不变）。
对齐：AGENTS.md §3（业务进 services 纯函数、薄端点）+ PRD §6 多轮上下文理解/可审计。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.models import Conversation, ConversationMessage
from office_agent_server.db import _now

#: 构造上下文时最多取的最近消息条数（防长会话上下文爆炸，也够支撑「上面那个」指代）
_CONTEXT_WINDOW = 8
#: 每条消息进上下文的截断长度（消息本身不截断，只截上下文摘要）
_CONTEXT_ITEM_LIMIT = 400
#: 上下文总长度上限（防 checkpoint/规划 prompt 膨胀）
_CONTEXT_TOTAL_LIMIT = 1600


def _user_scope(tenant: str, username: str) -> list[Any]:
    """租户 + 用户双条件（会话与消息共用同一隔离口径）。"""
    return [Conversation.tenant == tenant, Conversation.username == username]


async def list_conversations(
    db: AsyncSession, *, tenant: str, username: str
) -> list[dict[str, Any]]:
    """当前用户的会话列表（按更新时间倒序；不跨租户/用户）。"""
    rows = (
        (
            await db.execute(
                select(Conversation)
                .where(*_user_scope(tenant, username))
                .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_conversation_view(row) for row in rows]


async def get_conversation(
    db: AsyncSession, *, tenant: str, username: str, conversation_id: str
) -> dict[str, Any]:
    """会话详情（含消息列表）；不存在或越权统一 1004。"""
    conv = await _get_owned(db, tenant, username, conversation_id)
    rows = (
        (
            await db.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.tenant == tenant,
                    ConversationMessage.conversation_id == conversation_id,
                )
                .order_by(ConversationMessage.created_at, ConversationMessage.id)
            )
        )
        .scalars()
        .all()
    )
    return {**_conversation_view(conv), "messages": [_message_view(row) for row in rows]}


async def create_conversation(
    db: AsyncSession, *, tenant: str, username: str, title: str = ""
) -> dict[str, Any]:
    """新建会话（title 可空，默认「新对话」）。"""
    conv = Conversation(tenant=tenant, username=username, title=(title.strip() or "新对话")[:120])
    db.add(conv)
    await db.flush()
    return _conversation_view(conv)


async def delete_conversation(
    db: AsyncSession, *, tenant: str, username: str, conversation_id: str
) -> None:
    """删除会话并连带删除消息（仅当前用户可删自己的会话）。"""
    await _get_owned(db, tenant, username, conversation_id)
    await db.execute(
        delete(ConversationMessage).where(
            ConversationMessage.tenant == tenant,
            ConversationMessage.conversation_id == conversation_id,
        )
    )
    await db.execute(
        delete(Conversation).where(
            Conversation.tenant == tenant,
            Conversation.username == username,
            Conversation.id == conversation_id,
        )
    )


async def append_message(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    conversation_id: str,
    role: str,
    content: str,
    run_id: str = "",
) -> ConversationMessage:
    """落一条会话消息（用户/助手），并刷新会话 updated_at。

    先校验会话归属（不存在/越权统一 1004），杜绝向幽灵会话落消息。
    """
    await _get_owned(db, tenant, username, conversation_id)
    msg = ConversationMessage(
        tenant=tenant,
        conversation_id=conversation_id,
        role=role,
        content=content,
        run_id=run_id,
    )
    db.add(msg)
    await db.flush()
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.tenant == tenant,
                Conversation.id == conversation_id,
            )
        )
    ).scalar_one_or_none()
    if conv is not None:
        conv.updated_at = _now()
    return msg


async def recent_context(
    db: AsyncSession, *, tenant: str, conversation_id: str, window: int = _CONTEXT_WINDOW
) -> str:
    """最近 N 条用户消息的纯文本摘要（多轮上下文：新目标带着前文一起规划）。

    只取用户消息（助手回复含寒暄引导/示例词，混入会污染后续路由与规则匹配）；
    没有用户消息返回空串（首轮对话不带上下文，行为与现 POST /runs 完全一致）。
    """
    rows = (
        (
            await db.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.tenant == tenant,
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.role == "user",
                )
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                .limit(window)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return ""
    parts: list[str] = []
    total = 0
    for row in reversed(rows):  # 按时间正序拼；只取用户业务语境
        body = (row.content or "").strip().replace("\n", " ")
        if not body:
            continue
        body = body[:_CONTEXT_ITEM_LIMIT]
        if total + len(body) > _CONTEXT_TOTAL_LIMIT:
            break
        parts.append(f"用户：{body}")
        total += len(body)
    return "\n".join(parts)


async def _get_owned(
    db: AsyncSession, tenant: str, username: str, conversation_id: str
) -> Conversation:
    """取当前用户持有的会话；不存在/越权统一 1004（与 Task 越权口径一致）。"""
    row = (
        await db.execute(
            select(Conversation).where(
                Conversation.tenant == tenant,
                Conversation.username == username,
                Conversation.id == conversation_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "会话不存在或无权访问", 404)
    return row


def _conversation_view(conv: Conversation) -> dict[str, Any]:
    return {
        "id": conv.id,
        "title": conv.title,
        "created_at": conv.created_at.isoformat(sep=" ", timespec="seconds"),
        "updated_at": conv.updated_at.isoformat(sep=" ", timespec="seconds"),
    }


def _message_view(msg: ConversationMessage) -> dict[str, Any]:
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "run_id": msg.run_id,
        "created_at": msg.created_at.isoformat(sep=" ", timespec="seconds"),
    }
