"""人事行政工具（office.hr.attendance / checklist，对齐 PRD §2.8 前两项）。

职责（全读口径，免审）：
- office.hr.attendance：考勤 + 加班时长查询——考勤明细取 data_analysis 考勤台账
  （内置 + DOCS_DIR/data/attendance.csv 叠加，同口径复用），工时取工时台账
  （内置 + work.csv 叠加）；按人按日汇总出勤/迟到/请假天数与总工时，加班时长 =
  Σmax(0, 当日工时-8) 确定性计算；加班申请草稿不另起工具，直接走
  office.approval.draft（请假/加班类单必填校验追问已覆盖）；
- office.hr.checklist：入职指引（onboard）/ 离职交接清单（offboard）模板直出；
  offboard 的交接事项由入参 handover 给出（{item, owner?, due?}），缺项留
  【待补充】占位并在 unfilled 列出，绝不代编交接内容；交接事项提醒 = 清单 +
  截止原文，是否逾期交调用方按业务日期判断（本工具不判）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：台账取数只经 data_analysis 同一装载器（零复制）；CSV 叠加读失败只降级；
      数值全部原值直出，加班口径在响应注明；绝不 500。
对齐：AGENTS.md §3（降级不 500/溯源/不编造）；智能办公Agent 产品需求文档.md §2.8。
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .data_analysis import _DATASETS, _load_csv_overlay

SCOPE_READ = "office:read"

_STANDARD_DAY = 8.0

_ONBOARD_SECTIONS = (
    "证件与入职手续：身份证/学历证明原件核验，签劳动合同与保密协议",
    "账号与权限开通：邮箱/IM/考勤/项目管理账号，门禁卡领取",
    "培训安排：公司制度、报销流程、安全规范三门必修",
    "导师对接：指定入职导师，首周目标与座位/设备落实",
)

_OFFBOARD_SECTIONS = (
    "工作交接：进行中事项逐条移交责任人与截止，代码/文档权限移交",
    "资产归还：电脑/门禁卡/工位物品清点归还",
    "财务结算：报销清零、借款结清、最后薪资确认",
    "账号注销：各系统权限回收，离职证明开具",
)


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _to_hours(value: Any) -> float | None:
    """工时取值：数字直取（bool 拒绝），数字字符串可转，其余 None（不编造）。"""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


async def _hr_attendance(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.hr.attendance：考勤明细 + 工时加班汇总（person 必填，month 可选前缀过滤）。"""
    _ = ctx
    person = str(args.get("person") or "").strip()
    if not person:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 person 不能为空：请传入被查询人姓名")
    month = str(args.get("month") or "").strip()
    base_att = copy.deepcopy(_DATASETS["attendance"])
    base_work = copy.deepcopy(_DATASETS["work"])
    overlay_att, applied_att = await asyncio.to_thread(_load_csv_overlay, "attendance")
    overlay_work, applied_work = await asyncio.to_thread(_load_csv_overlay, "work")
    att_rows = [
        row
        for row in base_att + overlay_att
        if str(row.get("person") or "") == person
        and (not month or str(row.get("date") or "").startswith(month))
    ]
    work_rows = [
        row
        for row in base_work + overlay_work
        if str(row.get("person") or "") == person
        and (not month or str(row.get("date") or "").startswith(month))
    ]
    status_count: dict[str, int] = {}
    for row in att_rows:
        status = str(row.get("status") or "未知")
        status_count[status] = status_count.get(status, 0) + 1
    by_day: dict[str, float] = {}
    for row in work_rows:
        hours = _to_hours(row.get("hours"))
        if hours is None:
            continue
        day = str(row.get("date") or "未知日期")
        by_day[day] = by_day.get(day, 0.0) + hours
    total_hours = round(sum(by_day.values()), 2)
    overtime = round(sum(max(0.0, hours - _STANDARD_DAY) for hours in by_day.values()), 2)
    source = "builtin-demo" + ("+local-csv" if applied_att or applied_work else "")
    return {
        "person": person,
        "month": month or "全部",
        "attendance": att_rows,
        "status_count": status_count,
        "work_days": len(by_day),
        "total_hours": total_hours,
        "overtime_hours": overtime,
        "overtime_rule": f"加班时长=Σmax(0,当日工时-{_STANDARD_DAY:g})",
        "source": source,
        "fetched_at": _now_text(),
        "note": "加班申请草稿请走 office.approval.draft（加班类单必填校验追问已覆盖）",
    }


def _handover_or_raise(raw: Any) -> list[dict[str, str | None]]:
    """交接事项口径：对象数组，item 必填，owner/due 缺省 None（调用方见 null 即待补充）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 handover 必须是对象数组")
    items: list[dict[str, str | None]] = []
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise BusinessError(ErrorCode.PARAM_INVALID, f"handover 第 {index} 项必须是对象")
        item = str(entry.get("item") or "").strip()
        if not item:
            raise BusinessError(ErrorCode.PARAM_INVALID, f"handover 第 {index} 项 item 不能为空")
        owner = str(entry.get("owner") or "").strip() or None
        due = str(entry.get("due") or "").strip() or None
        items.append({"item": item[:200], "owner": owner, "due": due})
    return items


async def _hr_checklist(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.hr.checklist：入职指引 / 离职交接清单模板直出（缺项留白不代编）。"""
    _ = ctx
    scene = str(args.get("scene") or "").strip()
    if scene not in ("onboard", "offboard"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 scene 只能是 onboard/offboard（当前：{scene}）"
        )
    person = str(args.get("person") or "").strip()
    if scene == "onboard":
        sections = [f"【{person or '请补充姓名'}】入职指引", *list(_ONBOARD_SECTIONS)]
        return {
            "scene": scene,
            "sections": sections,
            "handover": [],
            "unfilled": [] if person else ["person"],
            "source": "builtin-template",
        }
    handover = _handover_or_raise(args.get("handover"))
    sections = [f"【{person or '请补充姓名'}】离职交接清单", *list(_OFFBOARD_SECTIONS)]
    reminders = [
        f"{entry['item']}——责任人{entry['owner'] or '【待补充】'}，"
        f"截止{entry['due'] or '【待补充】'}"
        for entry in handover
    ]
    unfilled = ([] if person else ["person"]) + (
        ["handover.owner/due（部分事项缺责任人或截止）"]
        if any(entry["owner"] is None or entry["due"] is None for entry in handover)
        else []
    )
    return {
        "scene": scene,
        "sections": sections,
        "handover": handover,
        "reminders": reminders,
        "unfilled": unfilled,
        "source": "builtin-template+input.handover",
        "note": "交接事项提醒只列原文与截止，是否逾期由调用方按业务日期判断",
    }


def specs() -> tuple[ToolSpec, ...]:
    """人事两工具的 ToolSpec（全读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.hr.attendance",
            scope=SCOPE_READ,
            description="考勤与加班时长查询：按人（可按月份前缀过滤）汇总出勤/迟到/请假天数与总工时，"
            "加班=Σmax(0,当日工时-8)；台账取数与 office.data.query 同口径",
            params={
                "type": "object",
                "properties": {
                    "person": {"type": "string", "description": "被查询人姓名", "minLength": 1},
                    "month": {"type": "string", "description": "月份前缀（例 2026-09，缺省全部）"},
                },
                "required": ["person"],
                "additionalProperties": False,
            },
            handler=_hr_attendance,
        ),
        ToolSpec(
            name="office.hr.checklist",
            scope=SCOPE_READ,
            description="入职指引/离职交接清单模板直出：onboard 四段手续，offboard 四段手续 + "
            "入参 handover 交接事项原文提醒（缺责任人/截止留占位并列 unfilled，不代编）",
            params={
                "type": "object",
                "properties": {
                    "scene": {
                        "type": "string",
                        "description": "场景（onboard 入职 / offboard 离职）",
                        "enum": ["onboard", "offboard"],
                    },
                    "person": {"type": "string", "description": "当事人姓名（缺省留占位）"},
                    "handover": {
                        "type": "array",
                        "description": "交接事项（offboard 用），元素 {item, owner?, due?}",
                        "items": {"type": "object"},
                    },
                },
                "required": ["scene"],
                "additionalProperties": False,
            },
            handler=_hr_checklist,
        ),
    )


def register_all() -> list[str]:
    """注册人事两工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
