"""办公资源预订（office.resource.query / book，对齐 PRD §2.8 第三项）。

职责：
- office.resource.query（office:read）：会议室/工位/公务车辆台账（内置演示 +
  DOCS_DIR/data/resources.csv 可选叠加）+ 预订台账（DOCS_DIR/data/
  resource_bookings.json）的占用查询：按类型过滤，给出指定日期每项资源的
  已订区间（判定只做区间重叠，不臆造占用人）；
- office.resource.book（office:write + 恒送审 + 幂等键必带）：按 resource_id +
  日期 + 起止（HH:MM）预订，区间重叠即 1001 拒收并给出冲突区间（不双订）；
  审批通过后才落预订台账（与 todo.create 同链）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler；
      预订台账读写锁 + asyncio.to_thread（affairs 同一磁盘纪律）。
红线：资源 id 经白名单校验（台账内才可订，不接受自由文本防幻影资源）；
      时间强格式（日期 YYYY-MM-DD + 起止 HH:MM，含糊拒绝）；缺资源/冲突一律
      中文可操作报错，绝不 500。
对齐：AGENTS.md §3（写动作恒送审/降级不 500/溯源）；智能办公Agent 产品需求文档.md §2.8。
"""

from __future__ import annotations

import asyncio
import copy
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

from .data_analysis import _load_csv_overlay

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: 内置演示资源台账（source=builtin-demo；真实部署用 DOCS_DIR/data/resources.csv 叠加）
_RESOURCE_ROWS: list[dict[str, Any]] = [
    {"id": "room-big", "type": "room", "name": "大会议室", "capacity": 20},
    {"id": "room-small", "type": "room", "name": "小会议室", "capacity": 6},
    {"id": "desk-a01", "type": "desk", "name": "A 区工位 01", "capacity": 1},
    {"id": "desk-a02", "type": "desk", "name": "A 区工位 02", "capacity": 1},
    {"id": "car-01", "type": "vehicle", "name": "公务车京A·001", "capacity": 5},
]

_STORE_LOCK = asyncio.Lock()


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _bookings_path() -> Path:
    """预订台账路径（DOCS_DIR/data/resource_bookings.json；惰性建目录）。"""
    root = Path(settings.DOCS_DIR).resolve() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "resource_bookings.json"


def _read_bookings() -> list[dict[str, Any]]:
    """读预订台账：缺文件即空；损坏转 1001 中文（绝不 500）。"""
    path = _bookings_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(ErrorCode.PARAM_INVALID, "预订台账损坏（无法解析）") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("bookings"), list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "预订台账损坏（根节点缺 bookings 数组）")
    return [b for b in payload["bookings"] if isinstance(b, dict)]


def _all_resources() -> tuple[list[dict[str, Any]], bool]:
    """内置台账 + CSV 叠加（叠加行缺 id/type 直接丢弃，不合流幻影资源）。"""
    rows = copy.deepcopy(_RESOURCE_ROWS)
    overlay, applied = _load_csv_overlay("resources")
    for row in overlay:
        if row.get("id") and row.get("type") in ("room", "desk", "vehicle"):
            rows.append(
                {
                    "id": str(row["id"]),
                    "type": str(row["type"]),
                    "name": str(row.get("name") or row["id"]),
                    "capacity": row.get("capacity") or 1,
                }
            )
    return rows, applied


def _overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """区间重叠判定（字符串 HH:MM 可直接比较，格式已由正则锁死）。"""
    return start_a < end_b and start_b < end_a


def _slot_or_raise(date: str, start: str, end: str) -> tuple[str, str, str]:
    """时间强格式：日期 YYYY-MM-DD + 起止 HH:MM，且起 < 止（含糊表述直接拒绝）。"""
    if not _RE_DATE.match(date or ""):
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 date 必须是 YYYY-MM-DD（当前：{date}）")
    if not _RE_HM.match(start or "") or not _RE_HM.match(end or ""):
        raise BusinessError(ErrorCode.PARAM_INVALID, f"起止必须是 HH:MM（当前：{start}-{end}）")
    if not start < end:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"起始必须早于结束（当前：{start}-{end}）")
    return date, start, end


async def _resource_query(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.resource.query：资源台账 + 指定日期已订区间（读，免审）。"""
    _ = ctx
    kind = str(args.get("type") or "").strip()
    if kind and kind not in ("room", "desk", "vehicle"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 type 只能是 room/desk/vehicle（当前：{kind}）"
        )
    date = str(args.get("date") or "").strip()
    if date and not _RE_DATE.match(date):
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 date 必须是 YYYY-MM-DD（当前：{date}）")
    resources, applied = await asyncio.to_thread(_all_resources)
    bookings = await asyncio.to_thread(_read_bookings)
    items = []
    for res in resources:
        if kind and res["type"] != kind:
            continue
        slots = [
            {
                "date": b.get("date"),
                "start": b.get("start"),
                "end": b.get("end"),
                "purpose": b.get("purpose"),
                "by": b.get("booked_by"),
            }
            for b in bookings
            if b.get("resource_id") == res["id"] and (not date or b.get("date") == date)
        ]
        items.append({**res, "booked_slots": slots})
    source = "builtin-demo" + ("+local-csv" if applied else "")
    return {
        "resources": items,
        "count": len(items),
        "date": date or "全部日期",
        "source": source,
        "fetched_at": _now_text(),
    }


async def _resource_book(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.resource.book：预订资源（审批通过后落台账；冲突即 1001 不双订）。"""
    resource_id = str(args.get("resource_id") or "").strip()
    date, start, end = _slot_or_raise(
        str(args.get("date") or ""), str(args.get("start") or ""), str(args.get("end") or "")
    )
    purpose = str(args.get("purpose") or "").strip()[:200]
    idem_key = str(args.get("idem_key") or "").strip()

    def _write() -> dict[str, Any]:
        resources, _ = _all_resources()
        target = next((r for r in resources if r["id"] == resource_id), None)
        if target is None:
            valid = "、".join(sorted({r["id"] for r in resources}))
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"未知资源「{resource_id}」（可用：{valid}，office.resource.query 可查）",
            )
        bookings = _read_bookings()
        clash = next(
            (
                b
                for b in bookings
                if b.get("resource_id") == resource_id
                and b.get("date") == date
                and _overlap(start, end, str(b.get("start")), str(b.get("end")))
            ),
            None,
        )
        if clash:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"「{target['name']}」{date} {clash.get('start')}-{clash.get('end')}"
                f"已被预订（{clash.get('purpose')}），请换时段",
            )
        record = {
            "resource_id": resource_id,
            "resource_name": target["name"],
            "date": date,
            "start": start,
            "end": end,
            "purpose": purpose or "未注明",
            "booked_by": ctx.username,
            "tenant": ctx.tenant,
            "booked_at": _now_text(),
            "idem_key": idem_key,
        }
        bookings.append(record)
        _bookings_path().write_text(
            json.dumps({"bookings": bookings}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return record

    async with _STORE_LOCK:
        record = await asyncio.to_thread(_write)
    return {**record, "note": "预订已落台账；占用查询请走 office.resource.query"}


def specs() -> tuple[ToolSpec, ...]:
    """资源两工具的 ToolSpec（query 读免审；book 写恒送审 + 幂等键）。"""
    type_prop = {
        "type": "string",
        "description": "资源类型（room 会议室 / desk 工位 / vehicle 公务车辆）",
        "enum": ["room", "desk", "vehicle"],
    }
    return (
        ToolSpec(
            name="office.resource.query",
            scope=SCOPE_READ,
            description="办公资源占用查询：会议室/工位/公务车辆台账（内置 + CSV 叠加）与指定日期已订区间",
            params={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "date": {"type": "string", "description": "日期 YYYY-MM-DD（缺省全部）"},
                },
                "additionalProperties": False,
            },
            handler=_resource_query,
        ),
        ToolSpec(
            name="office.resource.book",
            scope=SCOPE_WRITE,
            description="预订办公资源（写动作）：resource_id + 日期 + 起止 HH:MM，恒送审 + "
            "idem_key 必填；区间重叠即 1001 拒收并给出冲突区间，审批通过后落台账",
            params={
                "type": "object",
                "properties": {
                    "resource_id": {
                        "type": "string",
                        "description": "资源 id（office.resource.query 可查）",
                        "minLength": 1,
                    },
                    "date": {"type": "string", "description": "日期 YYYY-MM-DD", "minLength": 10},
                    "start": {"type": "string", "description": "起始 HH:MM", "minLength": 5},
                    "end": {"type": "string", "description": "结束 HH:MM", "minLength": 5},
                    "purpose": {"type": "string", "description": "用途说明", "maxLength": 200},
                    "idem_key": {
                        "type": "string",
                        "description": "幂等键（8-64 字符）",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                },
                "required": ["resource_id", "date", "start", "end", "idem_key"],
                "additionalProperties": False,
            },
            handler=_resource_book,
            idempotent=True,
            requires_approval=True,
            approval_action="office.resource.book",
        ),
    )


def register_all() -> list[str]:
    """注册资源两工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
