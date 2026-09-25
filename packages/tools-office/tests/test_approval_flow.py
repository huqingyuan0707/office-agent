"""审批流补齐工具单元测试（office.approval.submit / opinion，handler 直调）。

覆盖：提交批准后落台账与幂等回放（同键不双单）、执行期必填缺失诚实拒收、
      注册口径（写恒送审 + idem_key 必填）、审批说明/意见三档成稿与拒绝口径。
固件隔离：monkeypatch settings.DOCS_DIR 到 tmp_path（台账不污染真实文档目录）。
对齐：AGENTS.md §3（写动作恒送审/幂等）、§5（验证命令）；
      智能办公Agent 产品需求文档.md §2.3（一键发起/辅助优化）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import approval_opinion, approval_submit

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])

_EXPENSE = {"amount": 6800, "expense_date": "2026-09-20", "reason": "客户拜访打车与餐费"}


@pytest.fixture()
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """台账钉进临时目录（_ledger_path 实时读 settings.DOCS_DIR）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


def _ledger_tickets(docs_dir: Path) -> list[dict]:
    path = docs_dir / "data" / "approvals_ledger.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["tickets"]


# ---------------- office.approval.submit ----------------


async def test_submit_writes_ledger_after_approval(docs_dir: Path) -> None:
    """批准后执行（handler 只在 decide 通过后被调）：单据落台账并带溯源。"""
    data = await approval_submit._approval_submit(
        CTX, {"kind": "expense", "fields": _EXPENSE, "idem_key": "exp-2026-0001"}
    )
    assert data["replayed"] is False
    assert data["ticket"]["applicant"] == "alice"
    assert data["ticket"]["kind_label"] == "报销申请"
    assert data["ticket"]["status"] == "approved"
    assert data["source"] == "local-ledger:approvals_ledger.json"
    assert len(_ledger_tickets(docs_dir)) == 1


async def test_submit_same_idem_key_replays_without_double(docs_dir: Path) -> None:
    """同 (tenant, idem_key) 重放：返回原单 replayed=True，台账绝不双单。"""
    first = await approval_submit._approval_submit(
        CTX, {"kind": "expense", "fields": _EXPENSE, "idem_key": "exp-2026-0002"}
    )
    second = await approval_submit._approval_submit(
        CTX, {"kind": "expense", "fields": _EXPENSE, "idem_key": "exp-2026-0002"}
    )
    assert second["replayed"] is True
    assert second["ticket"]["ticket_id"] == first["ticket"]["ticket_id"]
    assert len(_ledger_tickets(docs_dir)) == 1


async def test_submit_rejects_incomplete_ticket(docs_dir: Path) -> None:
    """执行期复核：缺必填的单据诚实 1001 拒收，不落残缺台账。"""
    try:
        await approval_submit._approval_submit(
            CTX, {"kind": "leave", "fields": {"reason": "想休息"}, "idem_key": "lv-2026-0001"}
        )
    except BusinessError as exc:
        assert exc.code == 1001
        assert "请假类型" in exc.msg
    else:
        raise AssertionError("缺必填的提交应被 1001 拒收")
    assert _ledger_tickets(docs_dir) == []


def test_submit_spec_is_gated_write() -> None:
    """注册口径：office:write + 恒送审 + idem_key 必填（写动作免审路径不存在）。"""
    spec = approval_submit.specs()[0]
    assert spec.name == "office.approval.submit"
    assert spec.scope == "office:write"
    assert spec.requires_approval is True
    assert "idem_key" in spec.params["required"]


# ---------------- office.approval.opinion ----------------


async def test_opinion_applicant_quotes_fields_only() -> None:
    """审批说明：字段原值成稿，零编造字段外内容。"""
    data = await approval_opinion._approval_opinion(
        CTX, {"kind": "expense", "fields": _EXPENSE, "role": "applicant"}
    )
    assert data["role"] == "applicant"
    assert "本人alice提交报销申请" in data["text"]
    assert "6800" in data["text"]
    assert "客户拜访打车与餐费" in data["text"]


async def test_opinion_applicant_missing_fields_rejected() -> None:
    """必填缺失的说明成稿请求 → 1001 指向 draft 追问（不硬凑半截说明）。"""
    try:
        await approval_opinion._approval_opinion(
            CTX, {"kind": "trip", "fields": {"reason": "见客户"}, "role": "applicant"}
        )
    except BusinessError as exc:
        assert exc.code == 1001
        assert "出差目的地" in exc.msg
    else:
        raise AssertionError("缺必填的说明成稿应被拒")


async def test_opinion_reviewer_approve_carries_amount_tier() -> None:
    """复核意见（同意）：附金额分级提示，与 check 同一阈值口径。"""
    data = await approval_opinion._approval_opinion(
        CTX, {"kind": "expense", "fields": _EXPENSE, "role": "reviewer", "stance": "approve"}
    )
    assert "同意该报销申请" in data["text"]
    assert "分管副总" in data["text"]


async def test_opinion_reviewer_reject_requires_reason() -> None:
    """驳回意见必须附理由（与审批驳回同纪律）；supplement 列缺项。"""
    with pytest.raises(BusinessError):
        await approval_opinion._approval_opinion(
            CTX, {"kind": "expense", "fields": {}, "role": "reviewer", "stance": "reject"}
        )
    data = await approval_opinion._approval_opinion(
        CTX,
        {
            "kind": "expense",
            "fields": {"reason": "打车"},
            "role": "reviewer",
            "stance": "supplement",
        },
    )
    assert "报销金额" in data["text"]
    assert data["missing_fields"]
