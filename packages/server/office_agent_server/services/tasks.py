"""任务控制台服务（列表/统计/详情/操作/导出/审计，纯函数不含 FastAPI 对象）。

职责：
- list_tasks()：任务列表——按租户隔离，支持状态/类型/来源/关联对象过滤、
  关键词搜索、时间窗（今日/本周/本月）与 admin 全量口径；返回统一序列化视图。
- task_stats()：统计卡口径（总览 + 分状态 + 今日/本周/本月新增 + 执行中/失败等）。
- get_task()：单任务详情（input/output/error/checkpoint 解析 + 结果入口提取）。
- create_task()：登记后台任务（对话/文档中心/拆解/定时等来源入台的口子）。
- retry_task()/cancel_task()/delete_task()：操作 + 审计留痕（操作列动作）。
- export_task()：md/csv/xlsx 三种结果导出（base64 文本交付，不触盘）。

口径：
- 权限只在端点层判定（本人 = tenant+username；admin 才给 scope=all），本层只管查询/操作；
- agent.run 型任务（对话发起）无扩展列旧数据：name/source/ref_* 由本层按
  checkpoint/input 推断，绝不编造缺失状态；
- 失败任务的 error 列解析失败时给安全缺省（历史脏数据不拖垮控制台）。
对齐：AGENTS.md §3（分层红线/审计留痕/降级绝不 500）；PRD §6（任务拆解）
      与产品口径「Agent 任务执行控制台」（任务全生命周期 + 结果交付 + 审计）。
"""

from __future__ import annotations

import base64
import csv as _csv
import io
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.db import _now
from office_agent_server.models import Task, ToolCall

#: 任务来源取值的白名单（缺省 manual；扩展来源只在此登记）
SOURCE_CHOICES = {
    "chat": "对话办理",
    "docs": "文档中心",
    "breakdown": "任务拆解",
    "scheduled": "定时任务",
    "agent": "智能体",
    "manual": "手动登记",
}

#: 状态中文口径（含 modal 层展示）
STATUS_LABELS = {
    "pending": "排队中",
    "running": "执行中",
    "waiting_approval": "待确认",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

#: 结果入口探测字段（markdown 文档产物 / 链接 / 审批单，命中即给跳转入口）
_RESULT_KEYS = ("document", "report", "worklog", "minutes", "agenda", "markdown", "content")
_LINK_KEYS = ("url", "link", "file", "download_url")

#: 导出格式白名单（md/csv/xlsx 之外如实拒绝，不伪造 PDF/PPTX/SVG）
EXPORT_FORMATS = ("md", "csv", "xlsx")

_TASK_NAME_MAX = 80


def parse_json_text(raw: str, fallback: Any = None) -> Any:
    """JSON 文本 → 对象（脏数据给 fallback，绝不因历史脏列拖垮控制台）。"""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _task_name(row: Task, checkpoint: dict[str, Any]) -> str:
    """任务名：name 列优先；agent.run 按 checkpoint 的 agent+goal 推断，绝不编造。"""
    if row.name:
        return row.name
    if row.type == "agent.run":
        agent = str(checkpoint.get("agent") or "")
        goal = str(checkpoint.get("goal") or "")
        if agent and goal:
            return f"{agent}：{goal}"[:80]
        if goal:
            return goal[:80]
    return str(row.type or "未命名任务")


def _task_source(row: Task) -> str:
    """来源：source 列优先，agent.run 老数据归 chat/agent，绝不编造未知来源。"""
    if row.source:
        return row.source
    if row.type == "agent.run":
        return "agent"
    return "manual"


def _ref_of(row: Task, output: dict[str, Any]) -> tuple[str, str, str]:
    """关联对象三元组：列优先；agent.run 按审批挂起/输出里的 approval_id 推断。"""
    if row.ref_kind:
        return row.ref_kind, row.ref_id, row.ref_label
    approval_id = str(
        output.get("approval_id") or (output.get("pending_approval") or {}).get("approval_id") or ""
    )
    if approval_id:
        return "approval", approval_id, "待审批单"
    return "", "", ""


def _result_links(row: Task, output: dict[str, Any]) -> list[dict[str, str]]:
    """结果入口：输出里的文档产物/链接/审批单命中即给跳转提示（缺省空，绝不硬拼链接）。"""
    links: list[dict[str, str]] = []
    approval_id = str((output.get("pending_approval") or {}).get("approval_id") or "")
    if not approval_id:
        approval_id = str(output.get("approval_id") or "")
    if approval_id:
        links.append(
            {"kind": "approval", "label": "前往审批", "href": f"/approvals?id={approval_id}"}
        )
    for key in _RESULT_KEYS:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            links.append({"kind": "doc", "label": "查看结果", "href": ""})
            break
    for key in _LINK_KEYS:
        value = output.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            links.append({"kind": "link", "label": "打开链接", "href": value})
            break
    return links


def serialize_task(row: Task, *, detail: bool = False) -> dict[str, Any]:
    """单任务视图：列表与详情共用，detail 时补 input/output/error/checkpoint 原文。"""
    checkpoint = parse_json_text(row.checkpoint, {})
    output = parse_json_text(row.output, {}) or {}
    error = parse_json_text(row.error, {}) or {}
    ref_kind, ref_id, ref_label = _ref_of(row, output)
    entry: dict[str, Any] = {
        "id": row.id,
        "name": _task_name(row, checkpoint),
        "type": row.type,
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "source": _task_source(row),
        "source_label": SOURCE_CHOICES.get(_task_source(row), _task_source(row)),
        "progress": float(row.progress or 0),
        "username": row.username,
        "ref_kind": ref_kind,
        "ref_id": ref_id,
        "ref_label": ref_label,
        "result_links": _result_links(row, output),
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
        "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds"),
    }
    if detail:
        entry["input"] = checkpoint if isinstance(checkpoint, dict) else {}
        entry["output"] = output
        entry["error"] = error
        entry["checkpoint"] = checkpoint
        err_msg = error.get("message") or error.get("error")
        entry["error_hint"] = str(err_msg or "")[:300]
    return entry


def _advance_filters(
    stmt: Any,
    *,
    status: str,
    task_type: str,
    source: str,
    ref_kind: str,
    q: str,
    days: int,
) -> Any:
    """过滤条件统一追加（列表与统计共用，保持筛选口径一致）。"""
    if status:
        stmt = stmt.where(Task.status == status)
    if task_type:
        stmt = stmt.where(Task.type == task_type)
    if source:
        stmt = stmt.where(Task.source == source)
    if ref_kind:
        stmt = stmt.where(Task.ref_kind == ref_kind)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Task.name.like(like), Task.type.like(like), Task.ref_label.like(like))
        )
    if days and days > 0:
        cutoff = _now() - timedelta(days=days)
        stmt = stmt.where(Task.created_at >= cutoff)
    return stmt


def _scope_filter(stmt: Any, *, tenant: str, username: str, is_admin: bool, scope: str) -> Any:
    """租户/人员隔离：scope=all 且 admin 才看全租户；否则只看本人。"""
    if scope == "all" and is_admin:
        return stmt.where(Task.tenant == tenant)
    return stmt.where(Task.tenant == tenant, Task.username == username)


async def list_tasks(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    is_admin: bool,
    status: str = "",
    task_type: str = "",
    source: str = "",
    ref_kind: str = "",
    q: str = "",
    days: int = 0,
    scope: str = "mine",
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    """任务列表（scope=all 仅 admin）；返回 {rows(序列化), total, page, size, scope}。"""
    stmt = _scope_filter(
        select(Task), tenant=tenant, username=username, is_admin=is_admin, scope=scope
    )
    stmt = _advance_filters(
        stmt, status=status, task_type=task_type, source=source, ref_kind=ref_kind, q=q, days=days
    )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(desc(Task.created_at)).offset((page - 1) * size).limit(size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "rows": [serialize_task(row) for row in rows],
        "total": int(total),
        "page": page,
        "size": size,
        "scope": scope,
    }


async def task_stats(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    is_admin: bool,
    scope: str = "mine",
) -> dict[str, Any]:
    """统计卡：总览 + 分状态 + 今天/本周/本月新增（与列表同一过滤口径）。"""
    base = _scope_filter(
        select(Task), tenant=tenant, username=username, is_admin=is_admin, scope=scope
    )
    rows = (
        await db.execute(
            select(Task.status, func.count()).where(base.whereclause).group_by(Task.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    now = _now()
    # 今日/本周（本周从现在往回 7 天口径，避免周一/周日歧义）/本月
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async def _add_count(cutoff: datetime) -> int:
        total_stmt = _scope_filter(
            select(func.count()).select_from(Task),
            tenant=tenant,
            username=username,
            is_admin=is_admin,
            scope=scope,
        ).where(Task.created_at >= cutoff)
        return int((await db.execute(total_stmt)).scalar_one() or 0)

    stats = {
        "scope": scope,
        "total": sum(counts.values()),
        "by_status": counts,
        "today": await _add_count(day_start),
        "week": await _add_count(now - timedelta(days=7)),
        "month": await _add_count(now.replace(day=1)),
        "running": counts.get("running", 0) + counts.get("waiting_approval", 0),
        "failed": counts.get("failed", 0),
    }
    return stats


async def get_task(
    db: AsyncSession, *, tenant: str, username: str, is_admin: bool, task_id: str
) -> dict[str, Any]:
    """任务详情（越权/不存在同口径 1004）。"""
    row = await _get_task_row(
        db, tenant=tenant, username=username, is_admin=is_admin, task_id=task_id
    )
    return serialize_task(row, detail=True)


async def create_task(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    trace_id: str,
    name: str,
    task_type: str,
    source: str = "manual",
    ref_kind: str = "",
    ref_id: str = "",
    ref_label: str = "",
    status: str = "pending",
    progress: float = 0,
    note: str = "",
) -> dict[str, Any]:
    """登记后台任务（来源入台的口子：对话/文档等调用方把异步任务登记到控制台）。"""
    if source not in SOURCE_CHOICES:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"不支持的任务来源：{source}")
    if status not in STATUS_LABELS:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"不支持的任务状态：{status}")
    row = Task(
        tenant=tenant,
        username=username,
        type=task_type,
        status=status,
        progress=min(max(float(progress or 0), 0), 100),
        name=str(name or task_type)[:200],
        source=source,
        ref_kind=ref_kind,
        ref_id=ref_id,
        ref_label=ref_label,
        input=json.dumps({"note": note}, ensure_ascii=False),
    )
    db.add(row)
    await db.flush()
    await _audit(
        db,
        tenant=tenant,
        username=username,
        trace_id=trace_id,
        action="task.create",
        task_id=row.id,
        detail={"name": row.name, "type": row.type},
    )
    await db.commit()
    return serialize_task(row)


async def retry_task(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    is_admin: bool,
    trace_id: str,
    task_id: str,
) -> dict[str, Any]:
    """重试：仅失败/已取消/已完成可重放（终态外的先取消），恢复 pending 并清错误。"""
    row = await _get_task_row(
        db, tenant=tenant, username=username, is_admin=is_admin, task_id=task_id
    )
    if row.status in ("running", "pending", "waiting_approval"):
        raise BusinessError(ErrorCode.PARAM_INVALID, "任务尚未结束，不能重试（请先取消或等待）")
    row.status = "pending"
    row.progress = 0
    row.error = "{}"
    row.checkpoint = "{}"
    await _audit(
        db,
        tenant=tenant,
        username=username,
        trace_id=trace_id,
        action="task.retry",
        task_id=row.id,
        detail={"from": row.status},
    )
    await db.commit()
    return serialize_task(row)


async def cancel_task(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    is_admin: bool,
    trace_id: str,
    task_id: str,
) -> dict[str, Any]:
    """取消：仅排队/执行中/待确认可取消，终态拒绝（可操作提示）。"""
    row = await _get_task_row(
        db, tenant=tenant, username=username, is_admin=is_admin, task_id=task_id
    )
    if row.status in ("done", "failed", "cancelled"):
        raise BusinessError(ErrorCode.PARAM_INVALID, "任务已结束，不能取消")
    row.status = "cancelled"
    await _audit(
        db,
        tenant=tenant,
        username=username,
        trace_id=trace_id,
        action="task.cancel",
        task_id=row.id,
        detail={"from": row.status},
    )
    await db.commit()
    return serialize_task(row)


async def delete_task(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    is_admin: bool,
    trace_id: str,
    task_id: str,
) -> dict[str, Any]:
    """删除任务记录（操作留痕，审计不删）。"""
    row = await _get_task_row(
        db, tenant=tenant, username=username, is_admin=is_admin, task_id=task_id
    )
    await _audit(
        db,
        tenant=tenant,
        username=username,
        trace_id=trace_id,
        action="task.delete",
        task_id=row.id,
        detail={},
    )
    await db.delete(row)
    await db.commit()
    return {"deleted": task_id}


async def export_task(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    is_admin: bool,
    trace_id: str,
    task_id: str,
    fmt: str,
) -> dict[str, Any]:
    """任务结果导出：md/csv/xlsx → base64 文本交付（读入写出，不触盘）。

    内容口径：任务基本信息 + input/output/error 原文（缺省留空，绝不编造）。
    """
    if fmt not in EXPORT_FORMATS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"暂不支持导出 {fmt}，可用格式：{'/'.join(EXPORT_FORMATS)}（PDF/PPTX/SVG 请先转档）",
        )
    row = await _get_task_row(
        db, tenant=tenant, username=username, is_admin=is_admin, task_id=task_id
    )
    view = serialize_task(row, detail=True)
    content = _render_export(fmt, view)
    await _audit(
        db,
        tenant=tenant,
        username=username,
        trace_id=trace_id,
        action="task.export",
        task_id=row.id,
        detail={"format": fmt},
    )
    await db.commit()
    return {
        "format": fmt,
        "filename": str(view["name"])[:60] or row.id,
        "mime": {
            "md": "text/markdown",
            "csv": "text/csv",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }[fmt],
        "content": base64.b64encode(content).decode("ascii"),
    }


def _render_export(fmt: str, view: dict[str, Any]) -> bytes:
    """按格式渲染字节内容（md/csv 文本 UTF-8，xlsx 二进制）。"""
    title = str(view["name"])
    if fmt == "md":
        lines = [
            f"# 任务：{title}",
            "",
            f"- 任务 ID：{view['id']}",
            f"- 状态：{view['status_label']}",
            f"- 来源：{view['source_label']}",
            f"- 负责人：{view['username']}",
            f"- 创建时间：{view['created_at']}",
            f"- 更新时间：{view['updated_at']}",
            "",
            "## 输入",
            "",
            "```json",
            json.dumps(view.get("input") or {}, ensure_ascii=False, indent=2),
            "```",
            "",
            "## 输出",
            "",
            "```json",
            json.dumps(view.get("output") or {}, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
        if view.get("error"):
            lines += [
                "## 错误信息",
                "",
                "```json",
                json.dumps(view["error"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        return "\n".join(lines).encode("utf-8")
    if fmt == "csv":
        buffer = io.StringIO()
        writer = _csv.writer(buffer, lineterminator="\n")
        writer.writerow(["任务ID", "名称", "状态", "来源", "负责人", "进度", "创建时间", "备注"])
        err_hint = str(view.get("error_hint") or "")
        writer.writerow(
            [
                view["id"],
                title,
                view["status_label"],
                view["source_label"],
                view["username"],
                f"{view['progress']:.0f}",
                view["created_at"],
                err_hint,
            ]
        )
        return buffer.getvalue().encode("utf-8")
    # xlsx：openpyxl 可选依赖，缺库给可操作提示（绝不 500）
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font as _Font
    except ImportError:
        raise BusinessError(
            ErrorCode.TOOL_CALL_FAILED,
            "当前环境未安装 openpyxl，无法导出 Excel（请执行 pip install openpyxl 后重试）",
        ) from None
    book = Workbook()
    sheet = book.active
    sheet.title = (title[:24] or "任务导出").replace("[", "_").replace("]", "_")[:31]
    sheet.append(["字段", "内容"])
    for key in (
        "id",
        "name",
        "status_label",
        "source_label",
        "username",
        "progress",
        "created_at",
        "updated_at",
        "error_hint",
    ):
        sheet.append([key, str(view.get(key) or "")])
    if view.get("input"):
        sheet.append(
            ["input(JSON)", json.dumps(view["input"], ensure_ascii=False, indent=2, default=str)]
        )
    if view.get("output"):
        sheet.append(
            ["output(JSON)", json.dumps(view["output"], ensure_ascii=False, indent=2, default=str)]
        )
    sheet["A1"].font = _Font(bold=True)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


async def _get_task_row(
    db: AsyncSession, *, tenant: str, username: str, is_admin: bool, task_id: str
) -> Task:
    """按租户/人员取任务行（越权与不存在同口径 1004）。"""
    stmt = select(Task).where(Task.tenant == tenant)
    if not is_admin:
        stmt = stmt.where(Task.username == username)
    stmt = stmt.where(Task.id == task_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "任务不存在或无权访问", 404)
    return row


async def _audit(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    trace_id: str,
    action: str,
    task_id: str,
    detail: dict[str, Any],
) -> None:
    """任务操作留痕（tool_calls 审计表，只追加不改）。"""
    db.add(
        ToolCall(
            trace_id=trace_id or f"task:{task_id}",
            tenant=tenant,
            username=username,
            name=action,
            args=json.dumps({"task_id": task_id, **detail}, ensure_ascii=False, default=str),
            result="{}",
            latency_ms=0,
        )
    )
    await db.flush()
