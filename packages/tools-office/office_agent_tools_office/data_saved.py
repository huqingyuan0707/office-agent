"""常用数据查询的保存与一句话复用（office.data.query.save/list/run/delete，对齐 PRD §2.4 新增首项）。

职责：
- office.data.query.save（office:write + 恒送审 + 幂等键必带）：把「dataset +
  filters + limit」三件套落盘为 DOCS_DIR/data_queries/{name}.json（审批通过才
  真正写盘，与 office.template.save 同一套双人闭环）；
- office.data.query.list（office:read）：本人租户的已存查询清单；
- office.data.query.run（office:read）：按名复用——读存档取出口径后调
  data_analysis._data_query 真实重查（只复用口径，不返回过期行）；
- office.data.query.delete（office:write + 恒送审 + 幂等键必带）：按名删除。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler；
      写动作经审批闸门批准后以申请人身份执行落盘（与 office.todo.update 同链）；
      磁盘 IO 走 asyncio.to_thread（不阻塞事件循环）。
红线：查询名经 basename 守卫锁进 data_queries 目录（与 templates 同口径，绝不越界）；
      存档记 tenant，list/run 按租户隔离（跨租户不可见，误名与越权统一 1004 不泄露存在性）；
      同名重复保存 1001 拒绝（不静默覆盖）；存档损坏 1001 中文提示，绝不 500。
对齐：AGENTS.md §3（写动作恒送审/降级不 500/溯源）；
      智能办公Agent 产品需求文档.md §2.4「保存常用数据查询，支持一句话复用历史查询」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

from .data_analysis import _data_query, _dataset_or_raise

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

_NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff-]{1,64}$", re.UNICODE)
_MAX_LIMIT = 100
_STORE_LOCK = asyncio.Lock()


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _queries_dir() -> Path:
    """查询存档目录（DOCS_DIR/data_queries；惰性创建）。"""
    root = Path(settings.DOCS_DIR).resolve() / "data_queries"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _query_path(name: str) -> Path:
    """校验查询名并解析到 data_queries 目录内（拒绝路径分隔符/越界，与模板名同口径）。"""
    cleaned = str(name or "").strip()
    if not cleaned or not _NAME_PATTERN.match(cleaned) or ".." in cleaned:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            "查询名只能由中文/字母/数字/下划线/连字符组成（1-64 字符，不含路径分隔符）",
        )
    root = _queries_dir()
    target = (root / f"{cleaned}.json").resolve()
    if root not in target.parents:
        raise BusinessError(ErrorCode.PARAM_INVALID, "查询名非法：不允许越界访问查询目录")
    return target


def _filters_or_raise(raw: Any) -> dict[str, Any]:
    """过滤口径：缺省空对象；非对象拒绝（与 office.data.query 同口径，不静默忽略）。"""
    filters = raw or {}
    if not isinstance(filters, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 filters 必须是键值对对象")
    return dict(filters)


def _limit_or_raise(raw: Any) -> int:
    """行数口径：1-100 整数（与 office.data.query 同上限，避免存档带出幻影 limit）。"""
    limit = 20 if raw is None else raw
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 limit 必须是 1-{_MAX_LIMIT} 的整数")
    return limit


def _load_record(path: Path) -> dict[str, Any]:
    """读存档：非 JSON/非对象一律转 1001 中文（损坏不抛 500，不泄露堆栈）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"已存查询损坏（无法解析）：{path.stem}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("dataset") is None:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"已存查询损坏（缺 dataset 口径）：{path.stem}"
        )
    return payload


async def _query_save(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.query.save：查询口径落盘（审批通过后才被 decide 触发）。"""
    name = _query_path(str(args.get("name") or "")).stem
    dataset = _dataset_or_raise(args.get("dataset"))
    filters = _filters_or_raise(args.get("filters"))
    limit = _limit_or_raise(args.get("limit"))
    description = str(args.get("description") or "").strip()[:200]
    idem_key = str(args.get("idem_key") or "").strip()
    path = _query_path(name)
    payload = {
        "name": name,
        "tenant": ctx.tenant,
        "dataset": dataset,
        "filters": filters,
        "limit": limit,
        "description": description,
        "created_by": ctx.username,
        "saved_at": _now_text(),
        "idem_key": idem_key,
    }

    def _write() -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async with _STORE_LOCK:
        if path.exists():
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"查询「{name}」已存在；如需更新请先 office.data.query.delete 删除再保存（避免静默覆盖）",
            )
        await asyncio.to_thread(_write)
    return {
        "name": name,
        "file": f"data_queries/{path.name}",
        "dataset": dataset,
        "saved_at": payload["saved_at"],
        "note": "常用查询已保存；复用请调用 office.data.query.run（一句话按名重查）",
    }


async def _query_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.query.list：本人租户的已存查询清单（损坏文件跳过计数，不阻断）。"""
    _ = args
    root = _queries_dir()
    items: list[dict[str, Any]] = []
    skipped = 0
    for path in sorted(root.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        if not isinstance(record, dict) or record.get("tenant") != ctx.tenant:
            continue
        items.append(
            {
                "name": record.get("name") or path.stem,
                "dataset": record.get("dataset"),
                "filters": record.get("filters") or {},
                "limit": record.get("limit", 20),
                "description": record.get("description") or "",
                "saved_at": record.get("saved_at") or "",
            }
        )
    return {
        "queries": items,
        "count": len(items),
        "skipped": skipped,
        "source": "local-data-queries",
        "fetched_at": _now_text(),
    }


async def _query_run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.query.run：一句话复用——按名取出口径，用同一 handler 真实重查。"""
    path = _query_path(str(args.get("name") or ""))
    if not path.exists():
        raise BusinessError(
            ErrorCode.NOT_FOUND,
            f"未找到已保存的查询「{path.stem}」（office.data.query.list 可查清单）",
            404,
        )
    record = await asyncio.to_thread(_load_record, path)
    if record.get("tenant") != ctx.tenant:
        raise BusinessError(
            ErrorCode.NOT_FOUND,
            f"未找到已保存的查询「{path.stem}」（office.data.query.list 可查清单）",
            404,
        )
    result = await _data_query(
        ctx,
        {
            "dataset": _dataset_or_raise(record.get("dataset")),
            "filters": _filters_or_raise(record.get("filters")),
            "limit": _limit_or_raise(record.get("limit")),
        },
    )
    return {
        **result,
        "saved_as": record.get("name") or path.stem,
        "saved_at": record.get("saved_at") or "",
        "note": "结果按已保存查询口径实时查出（非过期快照）",
    }


async def _query_delete(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.query.delete：按名删除存档（审批通过后才被 decide 触发）。"""
    _ = ctx
    path = _query_path(str(args.get("name") or ""))

    def _remove() -> None:
        path.unlink()

    async with _STORE_LOCK:
        if not path.exists():
            raise BusinessError(
                ErrorCode.NOT_FOUND, f"未找到已保存的查询「{path.stem}」，无需删除", 404
            )
        await asyncio.to_thread(_remove)
    return {"name": path.stem, "deleted": True, "note": "常用查询已删除"}


def specs() -> tuple[ToolSpec, ...]:
    """常用查询四工具的 ToolSpec（save/delete 写恒送审 + 幂等键；list/run 读免审）。"""
    name_prop = {
        "type": "string",
        "description": "查询名称（中文/字母/数字/下划线/连字符，1-64 字符）",
        "minLength": 1,
        "maxLength": 64,
    }
    idem_prop = {
        "type": "string",
        "description": "幂等键（8-64 字符）",
        "minLength": 8,
        "maxLength": 64,
    }
    return (
        ToolSpec(
            name="office.data.query.save",
            scope=SCOPE_WRITE,
            description="保存常用数据查询（写动作）：记 name + dataset + filters + limit，"
            "恒送审 + idem_key 必填，审批通过后落盘，供 office.data.query.run 一句话复用",
            params={
                "type": "object",
                "properties": {
                    "name": name_prop,
                    "dataset": {
                        "type": "string",
                        "description": "数据集（与 office.data.query 同口径）",
                        "enum": ["project", "work", "sales", "attendance"],
                    },
                    "filters": {"type": "object", "description": "等值过滤条件（随查询一并保存）"},
                    "limit": {"type": "integer", "description": "返回行数上限（1-100，默认 20）"},
                    "description": {
                        "type": "string",
                        "description": "查询用途说明（一句话复用时帮助模型选对查询）",
                        "maxLength": 200,
                    },
                    "idem_key": idem_prop,
                },
                "required": ["name", "dataset", "idem_key"],
                "additionalProperties": False,
            },
            handler=_query_save,
            idempotent=True,
            requires_approval=True,
            approval_action="office.data.query.save",
        ),
        ToolSpec(
            name="office.data.query.list",
            scope=SCOPE_READ,
            description="查看已保存的常用数据查询清单：name/dataset/filters/limit/用途说明，只返回本人租户",
            params={"type": "object", "additionalProperties": False},
            handler=_query_list,
        ),
        ToolSpec(
            name="office.data.query.run",
            scope=SCOPE_READ,
            description="一句话复用历史查询：按 name 取出已保存的 dataset/filters/limit，"
            "用 office.data.query 同一实现实时重查，结果带 saved_as 溯源",
            params={
                "type": "object",
                "properties": {"name": name_prop},
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=_query_run,
        ),
        ToolSpec(
            name="office.data.query.delete",
            scope=SCOPE_WRITE,
            description="删除已保存的常用查询（写动作）：按 name 删除存档，恒送审 + idem_key 必填",
            params={
                "type": "object",
                "properties": {"name": name_prop, "idem_key": idem_prop},
                "required": ["name", "idem_key"],
                "additionalProperties": False,
            },
            handler=_query_delete,
            idempotent=True,
            requires_approval=True,
            approval_action="office.data.query.delete",
        ),
    )


def register_all() -> list[str]:
    """注册常用查询四工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
