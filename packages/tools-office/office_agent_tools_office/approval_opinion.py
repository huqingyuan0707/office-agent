"""审批说明/审批意见撰写（office.approval.opinion，对齐 PRD §2.3「智能撰写审批说明/审批意见」）。

职责：
- office.approval.opinion（office:read）：按单据类型与字段**确定性地**成稿两类文本——
  ①申请人视角（role=applicant）：审批说明（申请事由陈述，字段原值引用）；
  ②复核人视角（role=reviewer）：审批意见，按 stance 三档——
    approve（同意，附金额分级提示口径）/ reject（驳回理由必填，原文引用）/
    supplement（缺项追问，列出 missing_fields）。
  只组织入参原文，不编造字段外的数字与事实（数值不可编造红线的文本版）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
口径复用：金额分级阈值 import approval（>1000 部门负责人 / >5000 分管副总，唯一出处不另造）；
          模板与缺项校验复用 KIND_SPECS / missing_fields。
红线：读口径免审（成稿只是文本，提交仍走 office.approval.submit 恒送审）；纯本地实现。
对齐：AGENTS.md §3（数值不可编造）；智能办公Agent 产品需求文档.md §2.3（辅助优化）。
"""

from __future__ import annotations

from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_tools_office.approval import (
    DEPT_HEAD_AMOUNT,
    HIGH_RISK_AMOUNT,
    KIND_SPECS,
    kind_or_raise,
    missing_fields,
    to_amount,
)

SCOPE_READ = "office:read"

_ROLES = ("applicant", "reviewer")
_STANCES = ("approve", "reject", "supplement")


def _field_lines(kind: str, filled: dict[str, str]) -> list[str]:
    """已填字段 → 「标签=值」行（必填在前选填在后，顺序按模板固定，输出确定性）。"""
    spec = KIND_SPECS[kind]
    labels = spec["field_labels"]
    ordered = [*spec["required"], *spec["optional"]]
    return [f"{labels[key]}={filled[key]}" for key in ordered if key in filled]


def _applicant_text(kind: str, applicant: str, filled: dict[str, str]) -> str:
    """审批说明：本人陈述申请事项（字段原值引用，零发挥）。"""
    label = KIND_SPECS[kind]["label"]
    lines = "；".join(_field_lines(kind, filled))
    return f"本人{applicant}提交{label}：{lines}。请予审批。"


def _amount_hint(raw_fields: dict[str, Any]) -> str:
    """金额分级提示（只对含金额字段的单据给，口径与 check 一致）。

    取**原始入参**而非字符串化后的字段：to_amount 只认数字，字符串金额按「缺」处理
    （与 check 的「金额必须是数字」同一纪律，不猜 "6800" 这类文本）。
    """
    amount = to_amount(raw_fields.get("amount"))
    if amount is None:
        budget = to_amount(raw_fields.get("budget"))
        amount = budget
    if amount is None:
        return ""
    if amount > HIGH_RISK_AMOUNT:
        return f"金额 {amount} 超 {HIGH_RISK_AMOUNT}，需分管副总审批。"
    if amount > DEPT_HEAD_AMOUNT:
        return f"金额 {amount} 超 {DEPT_HEAD_AMOUNT}，需部门负责人审批。"
    return ""


def _reviewer_text(
    kind: str,
    stance: str,
    filled: dict[str, str],
    missing: list[str],
    reason: str,
    raw_fields: dict[str, Any],
) -> str:
    """审批意见三档成稿：同意/驳回（理由必填）/补件（列缺项）。"""
    label = KIND_SPECS[kind]["label"]
    if stance == "reject":
        return f"不同意该{label}。理由：{reason}"
    if stance == "supplement":
        return f"该{label}暂不成稿，请申请人补充：{'、'.join(missing)}。补齐后重新提交。"
    head = f"同意该{label}（{'；'.join(_field_lines(kind, filled)) or '字段见单据'}）。"
    hint = _amount_hint(raw_fields)
    return head + (hint if hint else "")


async def _approval_opinion(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.approval.opinion：确定性成稿审批说明/审批意见（不编造字段外内容）。"""
    kind = kind_or_raise(args.get("kind"))
    role = str(args.get("role") or "applicant").strip()
    if role not in _ROLES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 role 只能是 {'、'.join(_ROLES)}（当前：{role}）"
        )
    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 fields 必须是键值对对象")
    filled = {key: str(value).strip() for key, value in fields.items() if str(value).strip()}
    missing = missing_fields(kind, filled)

    if role == "applicant":
        if missing:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"必填项缺失无法成稿说明：{'、'.join(missing)}（先用 office.approval.draft 追问补齐）",
            )
        text = _applicant_text(kind, ctx.username, filled)
        stance = ""
    else:
        stance = str(args.get("stance") or "approve").strip()
        if stance not in _STANCES:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"参数 stance 只能是 {'、'.join(_STANCES)}（当前：{stance}）",
            )
        reason = str(args.get("reason") or "").strip()
        if stance == "reject" and not reason:
            raise BusinessError(ErrorCode.PARAM_INVALID, "驳回意见必须附理由（reason 不能为空）")
        if stance == "supplement" and not missing:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, "该单据必填项已齐备，无需补件意见（可改用 approve/reject）"
            )
        text = _reviewer_text(kind, stance, filled, missing, reason, fields)
    return {
        "kind": kind,
        "kind_label": KIND_SPECS[kind]["label"],
        "role": role,
        "stance": stance,
        "text": text,
        "missing_fields": missing,
        "note": "成稿只引用入参原文，未添加字段外事实；提交仍需走 office.approval.submit（恒送审）",
        "source": "local-template",
    }


def specs() -> tuple[ToolSpec, ...]:
    """office.approval.opinion 的 ToolSpec（读口径，免审）。"""
    return (
        ToolSpec(
            name="office.approval.opinion",
            scope=SCOPE_READ,
            description="撰写审批说明/审批意见：申请人说明（role=applicant，字段原值成稿）与复核人意见（role=reviewer，stance=approve/reject/supplement 三档，驳回必须附理由），金额分级提示同 check 口径；确定性模板零编造",
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "单据类型（leave/expense/purchase/overtime/trip）",
                        "enum": sorted(KIND_SPECS),
                    },
                    "fields": {"type": "object", "description": "单据字段键值对"},
                    "role": {
                        "type": "string",
                        "description": "视角（applicant=审批说明 / reviewer=审批意见）",
                        "enum": list(_ROLES),
                    },
                    "stance": {
                        "type": "string",
                        "description": "复核立场（仅 reviewer：approve 同意/reject 驳回/supplement 补件）",
                        "enum": list(_STANCES),
                    },
                    "reason": {"type": "string", "description": "驳回理由（stance=reject 必填）"},
                },
                "required": ["kind", "fields"],
                "additionalProperties": False,
            },
            handler=_approval_opinion,
        ),
    )


def register_all() -> list[str]:
    """注册审批说明/意见撰写工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
