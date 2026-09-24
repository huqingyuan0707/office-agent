"""合规风险检测工具单元测试（handler 直调，不起 HTTP 服务）。

覆盖：三类规则命中（隐私/违规用语/泄密凭据）、凭据脱敏摘录、无命中 passed、
      空文本与未知场景 1001 拒绝、注册口径（读工具免审批）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.7/§4.1（V1.2 合规检测）。
"""

from __future__ import annotations

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_tools_office import compliance

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


# ---------------- 三类规则 ----------------


async def test_privacy_hits_phone_id_and_bank() -> None:
    data = await compliance._compliance_scan(
        CTX,
        {
            "text": "联系人张三 13812345678，证件 110101199003077770，卡号 6222020200112233445。",
            "scene": "email",
        },
    )
    rules = {hit["rule"] for hit in data["hits"]}
    assert {"手机号", "身份证号", "银行卡号"} <= rules
    assert data["passed"] is False
    assert data["counts"]["privacy"] == 3
    assert any("脱敏" in tip for tip in data["tips"])


async def test_id_card_not_double_counted_as_bank_card() -> None:
    data = await compliance._compliance_scan(CTX, {"text": "证件号 110101199003077770 请核对"})
    rules = [hit["rule"] for hit in data["hits"]]
    assert rules.count("身份证号") == 1
    assert "银行卡号" not in rules


async def test_absolute_words_hit() -> None:
    data = await compliance._compliance_scan(CTX, {"text": "本品全网最低价，百分百正品保障。"})
    assert data["counts"]["words"] >= 2
    assert any(hit["rule"] == "绝对化用语" for hit in data["hits"])


async def test_secret_hit_is_masked() -> None:
    data = await compliance._compliance_scan(
        CTX, {"text": "配置如下 password=SuperSecret123 请查收"}
    )
    assert data["counts"]["leak"] == 1
    excerpt = data["hits"][0]["excerpt"]
    assert "SuperSecret123" not in excerpt
    assert excerpt.startswith("password=")


async def test_clean_text_passes() -> None:
    data = await compliance._compliance_scan(CTX, {"text": "本周例会讨论了版本计划，按期推进。"})
    assert data["passed"] is True
    assert data["hits"] == []
    assert data["tips"] == []


async def test_email_scene_appends_reject_tip() -> None:
    data = await compliance._compliance_scan(
        CTX, {"text": "我的手机号 13912345678 联系我", "scene": "email"}
    )
    assert any("未通过" in tip for tip in data["tips"])


# ---------------- 参数口径 ----------------


async def test_rejects_blank_text() -> None:
    try:
        await compliance._compliance_scan(CTX, {"text": "   "})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("空文本应被 1001 拒绝")


async def test_rejects_unknown_scene() -> None:
    try:
        await compliance._compliance_scan(CTX, {"text": "内容", "scene": "chat"})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("未知场景应被 1001 拒绝")


# ---------------- 注册口径 ----------------


def test_spec_is_read_without_approval() -> None:
    (spec,) = compliance.specs()
    assert spec.name == "office.compliance.scan"
    assert spec.scope == "office:read"
    assert spec.requires_approval is False
