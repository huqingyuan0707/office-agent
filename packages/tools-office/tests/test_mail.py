"""邮件智能处理工具单元测试（office.mail.*，handler 直调）。

覆盖：四类归类的关键词命中与默认口径、回复草稿占位与不代编、行动项三字段
      （owner/due 提不出置 null）、预审命中摘录（隐私/语气/凭据脱敏）与通过口径。
对齐：智能办公Agent 产品需求文档.md §2.7；AGENTS.md §5（验证命令）。
"""

from __future__ import annotations

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_tools_office import mail

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


# ---------------- office.mail.classify ----------------


async def test_classify_four_categories() -> None:
    """四类关键词各命中一条：category 与 counts 如实分桶。"""
    result = await mail._mail_classify(
        CTX,
        {
            "emails": [
                {"subject": "限时优惠", "body": "点击链接领取"},
                {"subject": "合同盖章", "body": "请于周五前办理"},
                {"subject": "方案确认", "body": "是否可行，盼复"},
                {"subject": "例会安排", "body": "特此通知，请知会全员"},
            ]
        },
    )
    categories = [item["category"] for item in result["results"]]
    assert categories == ["spam", "action", "reply", "info"]
    assert result["counts"] == {"spam": 1, "action": 1, "reply": 1, "info": 1}


async def test_classify_unmatched_is_informational() -> None:
    """无关键词命中归一般告知，matched_keywords 为空（不推断）。"""
    result = await mail._mail_classify(
        CTX, {"emails": [{"subject": "会议纪要归档", "body": "已上传网盘"}]}
    )
    assert result["results"][0]["category"] == "informational"
    assert result["results"][0]["matched_keywords"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"emails": []},
        {"emails": ["不是对象"]},
        {"emails": [{"subject": "", "body": ""}]},
        {"emails": [{"subject": "x", "body": "y"}] * 21},
    ],
)
async def test_classify_rejects_bad_input(payload: dict) -> None:
    """空数组/非对象元素/双空字段/超上限一律 1001 可操作报错。"""
    with pytest.raises(BusinessError):
        await mail._mail_classify(CTX, payload)


# ---------------- office.mail.reply_draft ----------------


async def test_reply_draft_with_points() -> None:
    """要点原样进正文（编号列表），主题加 Re: 前缀，署名恒为占位。"""
    result = await mail._mail_reply_draft(
        CTX,
        {
            "subject": "合作报价",
            "sender_name": "王经理",
            "tone": "formal",
            "key_points": ["报价含税", "交期两周"],
        },
    )
    assert result["reply_subject"] == "Re: 合作报价"
    assert "王经理，您好：" in result["draft"]
    assert "1. 报价含税" in result["draft"] and "2. 交期两周" in result["draft"]
    assert result["placeholders"] == ["署名"]


async def test_reply_draft_missing_fields_leave_placeholders() -> None:
    """缺称呼/缺要点：草稿留占位、placeholders 如实列出，绝不代编。"""
    result = await mail._mail_reply_draft(CTX, {"subject": "咨询"})
    assert "【请补充正文要点】" in result["draft"]
    assert set(result["placeholders"]) == {"称呼", "正文要点", "署名"}


async def test_reply_draft_rejects_bad_tone_and_subject() -> None:
    with pytest.raises(BusinessError):
        await mail._mail_reply_draft(CTX, {"subject": "x", "tone": "aggressive"})
    with pytest.raises(BusinessError):
        await mail._mail_reply_draft(CTX, {"subject": ""})


# ---------------- office.mail.action_items ----------------


async def test_action_items_extracts_owner_and_due() -> None:
    """三形态责任人 + 两类截止（ISO 日期 / 中文时间）逐句提取。"""
    result = await mail._mail_action_items(
        CTX,
        {"body": "@张三 请在 2026-09-30 前完成方案定稿。李四负责整理数据，本周五前提交。"},
    )
    assert result["count"] == 2
    first, second = result["items"]
    assert first["owner"] == "张三" and first["due"] == "2026-09-30"
    assert second["owner"] == "李四" and second["due"] == "本周五"


async def test_action_items_null_fields_not_invented() -> None:
    """原文没给责任人/截止：置 null 不臆造。"""
    result = await mail._mail_action_items(CTX, {"body": "请提交季度报告"})
    item = result["items"][0]
    assert item["owner"] is None and item["due"] is None


async def test_action_items_skips_non_action_sentences() -> None:
    """无动作词的句子不进清单；空正文 1001。"""
    result = await mail._mail_action_items(CTX, {"body": "你好。最近顺利吗"})
    assert result["count"] == 0
    with pytest.raises(BusinessError):
        await mail._mail_action_items(CTX, {"body": ""})


# ---------------- office.mail.precheck ----------------


async def test_precheck_clean_passes() -> None:
    """正常外发邮件：passed=True、need_confirm=False、零命中。"""
    result = await mail._mail_precheck(CTX, {"text": "附件为本季度交付计划，欢迎提出修改意见。"})
    assert result["passed"] is True and result["need_confirm"] is False
    assert result["hits"] == []


async def test_precheck_hits_privacy_and_tone() -> None:
    """手机号 + 命令催促语气：各记一条命中并要求二次确认。"""
    result = await mail._mail_precheck(
        CTX, {"text": "我的手机号13812345678，你们必须立刻处理，否则投诉。"}
    )
    categories = {hit["category"] for hit in result["hits"]}
    assert "privacy" in categories and "tone" in categories
    assert result["need_confirm"] is True


async def test_precheck_secret_value_masked() -> None:
    """凭据命中摘录脱敏（值不落扫描结果，防二次泄露）。"""
    result = await mail._mail_precheck(CTX, {"text": "系统口令 password=supersecret123 请查收"})
    leak = [hit for hit in result["hits"] if hit["category"] == "leak"]
    assert leak and "supersecret123" not in leak[0]["excerpt"]
