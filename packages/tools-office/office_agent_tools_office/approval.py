"""审批智能辅助工具（office.approval.draft / check / invoice.extract，对齐 PRD §2.3 V1.1 第三项）。

职责：
- office.approval.draft（office:read）：五类常用单（请假/报销/采购/加班/出差）草稿生成——
  按模板校验必填项，缺项在 missing_fields 给追问提示（PRD §2.3 辅助优化：自动校验表单必填项）；
- office.approval.check（office:read）：合规自查——复核必填 + 金额分级审批提示
  （>1000 部门负责人 / >5000 分管副总，与 kb 内置报销条目同口径）+ 高危二次确认标记
  （need_confirm，PRD §2.3 新增：避免误提交大额申请）；
- office.invoice.extract（office:read）：发票信息提取——从 OCR 文本中正则提取金额/
  发票号码/开票日期/销售方，供报销单自动填充；无命中 degraded 留白不编造
  （文本来源为 ocr.image，PRD §2.3 新增：发票识别）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
公开原语（KIND_SPECS / kind_or_raise / missing_fields / to_amount）供同包
approval_submit.py（一键提交落台账）与 approval_opinion.py（说明/意见撰写）复用，模板口径唯一出处。
红线：三工具全是读口径（免审批，只出草稿与意见，不直接发起审批）；纯本地实现，
      不触及 ORM / FastAPI；合规阈值与知识库口径一致，不另造标准。
对齐：AGENTS.md §3（分层/数值不可编造类推金额不编造）；智能办公Agent 产品需求文档.md
      §2.3（审批智能助手——一键发起/辅助优化/发票识别/高危二次确认；催办复用
      notifications 扫描链，不重复造）。
"""

from __future__ import annotations

import re
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

SCOPE_READ = "office:read"

#: 五类常用审批单模板：必填项（required）+ 选填项（optional）+ 中文标签
KIND_SPECS: dict[str, dict[str, Any]] = {
    "leave": {
        "label": "请假申请",
        "required": ("leave_type", "start_date", "end_date", "reason"),
        "optional": ("handover",),
        "field_labels": {
            "leave_type": "请假类型",
            "start_date": "开始日期",
            "end_date": "结束日期",
            "reason": "请假事由",
            "handover": "工作交接人",
        },
    },
    "expense": {
        "label": "报销申请",
        "required": ("amount", "expense_date", "reason"),
        "optional": ("category", "invoice_no"),
        "field_labels": {
            "amount": "报销金额",
            "expense_date": "费用发生日期",
            "reason": "报销事由",
            "category": "费用类别",
            "invoice_no": "发票号码",
        },
    },
    "purchase": {
        "label": "采购申请",
        "required": ("item", "quantity", "budget", "reason"),
        "optional": ("supplier",),
        "field_labels": {
            "item": "采购物品",
            "quantity": "数量",
            "budget": "预算金额",
            "reason": "采购事由",
            "supplier": "意向供应商",
        },
    },
    "overtime": {
        "label": "加班申请",
        "required": ("date", "hours", "reason"),
        "optional": ("task",),
        "field_labels": {
            "date": "加班日期",
            "hours": "加班时长（小时）",
            "reason": "加班事由",
            "task": "加班任务",
        },
    },
    "trip": {
        "label": "出差申请",
        "required": ("destination", "start_date", "end_date", "reason"),
        "optional": ("traffic",),
        "field_labels": {
            "destination": "出差目的地",
            "start_date": "开始日期",
            "end_date": "结束日期",
            "reason": "出差事由",
            "traffic": "交通方式",
        },
    },
}

#: 高危二次确认线（金额：误提交大额申请的拦截口）；公开供 approval_opinion 复用同一分级口径
HIGH_RISK_AMOUNT = 5000.0
DEPT_HEAD_AMOUNT = 1000.0

#: 发票文本提取正则（只摘录原文命中，不推断）
_AMOUNT_RES = (
    re.compile(r"[¥￥]\s?([\d,]+\.\d{1,2})"),
    re.compile(r"(?:合计|总计|金额|小写)[：:]\s?[¥￥]?\s?([\d,]+\.\d{1,2})"),
)
_INVOICE_NO_RES = (
    re.compile(r"发票号码[：:]\s?(\d{8,20})"),
    re.compile(r"发票代码[：:]\s?(\d{10,12})"),
)
_DATE_RES = (
    re.compile(r"(\d{4}年\d{1,2}月\d{1,2}日)"),
    re.compile(r"开票日期[：:]\s?(\d{4}-\d{1,2}-\d{1,2})"),
)
_SELLER_RE = re.compile(r"(?:销售方|销货方|收款方)[：:]\s?([^\n，,]{2,30})")


def kind_or_raise(kind: str) -> str:
    """单据类型口径：只认五类常用单（未知名中文可操作报错）。"""
    name = str(kind or "").strip()
    if name not in KIND_SPECS:
        valid = "、".join(sorted(KIND_SPECS))
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 kind 只能是 {valid}（当前：{kind}）")
    return name


def missing_fields(kind: str, fields: dict[str, Any]) -> list[str]:
    """必填缺项追问：返回缺失字段的中文标签（全齐返回空列表）。"""
    spec = KIND_SPECS[kind]
    labels = spec["field_labels"]
    return [
        str(labels[key]) for key in spec["required"] if str(fields.get(key) or "").strip() == ""
    ]


async def _approval_draft(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.approval.draft：生成审批单草稿 + 缺项追问（只出草稿，不发起审批）。"""
    kind = kind_or_raise(args.get("kind"))
    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 fields 必须是键值对对象")
    spec = KIND_SPECS[kind]
    filled = {key: str(value).strip() for key, value in fields.items() if str(value).strip()}
    missing = missing_fields(kind, filled)
    summary = f"{spec['label']}草稿（申请人：{ctx.username}）：" + "、".join(
        f"{spec['field_labels'][k]}={filled.get(k, '待补充')}" for k in spec["required"]
    )
    return {
        "kind": kind,
        "kind_label": spec["label"],
        "applicant": ctx.username,
        "fields": filled,
        "missing_fields": missing,
        "ready": not missing,
        "next_hint": ("草稿已齐备，可提交审批" if not missing else f"请补充：{'、'.join(missing)}"),
        "summary": summary,
    }


def to_amount(value: Any) -> float | None:
    """金额取值：数字原值直取（bool 拒绝）；非数字给 None 由调用方判缺项。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


async def _approval_check(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.approval.check：必填复核 + 金额分级审批提示 + 高危二次确认标记。"""
    _ = ctx
    kind = kind_or_raise(args.get("kind"))
    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 fields 必须是键值对对象")
    issues: list[str] = []
    hints: list[str] = []
    need_confirm = False
    missing = missing_fields(kind, fields)
    if missing:
        issues.append(f"必填项缺失：{'、'.join(missing)}")
    if kind == "expense":
        amount = to_amount(fields.get("amount"))
        if amount is None and "报销金额" not in missing:
            issues.append("报销金额必须是数字（不含 ¥ 符号与逗号以外的字符请先清洗）")
        elif amount is not None:
            if amount > HIGH_RISK_AMOUNT:
                need_confirm = True
                hints.append(
                    f"金额 {amount} 超过 {HIGH_RISK_AMOUNT}：需分管副总审批，"
                    "且提交前必须二次确认（高危操作防误提交）"
                )
            elif amount > DEPT_HEAD_AMOUNT:
                hints.append(f"金额 {amount} 超过 {DEPT_HEAD_AMOUNT}：需部门负责人审批")
            if not str(fields.get("invoice_no") or "").strip():
                hints.append("未附发票号码：建议先用 office.invoice.extract 提取后补入")
    if kind == "leave":
        start = str(fields.get("start_date") or "").strip()
        end = str(fields.get("end_date") or "").strip()
        if start and end and start > end:
            issues.append("开始日期晚于结束日期：请核对请假区间")
        if str(fields.get("leave_type") or "").strip() == "病假":
            hints.append("病假请假：记得按制度补交医院证明")
    return {
        "kind": kind,
        "passed": not issues,
        "issues": issues,
        "hints": hints,
        "need_confirm": need_confirm,
        "confirm_tip": ("高危操作：请二次确认金额与收款信息无误后再提交" if need_confirm else ""),
    }


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> str:
    """取首个正则命中组（无命中返回空串，不编造）。"""
    for pattern in patterns:
        matched = pattern.search(text)
        if matched:
            return matched.group(1).strip()
    return ""


async def _invoice_extract(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.invoice.extract：从 OCR 文本提取发票四要素（只摘录，不推断）。"""
    _ = ctx
    text = str(args.get("text") or "").strip()
    if not text:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 text 不能为空：请传入 OCR 识别文本")
    amount_raw = _first_match(_AMOUNT_RES, text).replace(",", "")
    invoice_no = _first_match(_INVOICE_NO_RES, text)
    bill_date = _first_match(_DATE_RES, text)
    seller = _first_match((_SELLER_RE,), text)
    extracted = {
        "amount": float(amount_raw) if amount_raw else "",
        "invoice_no": invoice_no,
        "bill_date": bill_date,
        "seller": seller,
    }
    filled = {key: value for key, value in extracted.items() if value != ""}
    return {
        "extracted": extracted,
        "filled_count": len(filled),
        "degraded": not filled,
        "degraded_reason": (
            "文本中未命中发票要素：请确认传入的是发票 OCR 文本，或换清晰图片重识"
            if not filled
            else ""
        ),
        "fill_hint": "提取结果可直接填入报销单 amount/invoice_no（仍需人工核对后提交）",
        "source": "input.text",
    }


def specs() -> tuple[ToolSpec, ...]:
    """三个审批辅助工具的 ToolSpec（全读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.approval.draft",
            scope=SCOPE_READ,
            description="生成常用审批单草稿：请假/报销/采购/加班/出差五类模板，自动校验必填项，缺项在 missing_fields 给追问提示；只出草稿不发起审批",
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "单据类型（leave/expense/purchase/overtime/trip）",
                        "enum": ["leave", "expense", "purchase", "overtime", "trip"],
                    },
                    "fields": {"type": "object", "description": "已填字段键值对"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
            handler=_approval_draft,
        ),
        ToolSpec(
            name="office.approval.check",
            scope=SCOPE_READ,
            description="审批合规自查：必填复核 + 金额分级审批提示（>1000部门负责人/>5000分管副总）+ 高危二次确认标记 need_confirm",
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "单据类型（leave/expense/purchase/overtime/trip）",
                        "enum": ["leave", "expense", "purchase", "overtime", "trip"],
                    },
                    "fields": {"type": "object", "description": "待查字段键值对"},
                },
                "required": ["kind", "fields"],
                "additionalProperties": False,
            },
            handler=_approval_check,
        ),
        ToolSpec(
            name="office.invoice.extract",
            scope=SCOPE_READ,
            description="从发票 OCR 文本提取金额/发票号码/开票日期/销售方，供报销单自动填充；无命中如实 degraded，需人工核对",
            params={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "发票 OCR 识别文本", "minLength": 1},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_invoice_extract,
        ),
    )


def register_all() -> list[str]:
    """注册三个审批辅助工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
