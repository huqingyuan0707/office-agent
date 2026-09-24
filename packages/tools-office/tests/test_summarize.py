"""文本处理工具单测（handler 直调，不起 HTTP 服务）。

覆盖：单篇抽取摘要（原句拼接/要点/覆盖计数）、多篇逐篇一句 + 联合摘要、空文本与
      非法参数拒绝、格式统一（空白归一 + 改动计数 + 正文不动）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.1（文档处理）。
"""

from __future__ import annotations

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_tools_office import summarize

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])

_DEMO = "项目背景介绍。本期完成审批闭环上线，数值一致率百分之百。本期完成审批闭环上线后，复核效率明显提升。下周计划接入考勤台账。"


async def test_summarize_single_picks_top_sentences_verbatim() -> None:
    """单篇摘要：取 top 原句按原文顺序拼接（出现频次最高的句子优先）。"""
    data = await summarize._text_summarize(CTX, {"text": _DEMO, "max_sentences": 2})
    assert data["mode"] == "single"
    assert data["picked"] == 2
    assert data["total_sentences"] == 4
    assert "本期完成审批闭环上线" in data["summary"]
    assert len(data["key_points"]) == 2
    for point in data["key_points"]:
        assert point in _DEMO  # 摘录必为原文原句


async def test_summarize_multi_one_liner_per_doc() -> None:
    """多篇整合：逐篇一句话 + 联合摘要拼接。"""
    data = await summarize._text_summarize(
        CTX, {"texts": ["第一篇讲日报上线成功。", "第二篇讲审批提速明显。"]}
    )
    assert data["mode"] == "multi"
    assert data["doc_count"] == 2
    assert data["per_doc"][0]["one_liner"] == "第一篇讲日报上线成功。"
    assert "第一篇讲日报上线成功。" in data["joint_summary"]


async def test_summarize_rejects_empty_and_bad_count() -> None:
    """空文本/非法取句数拒绝。"""
    for args in ({"text": "  "}, {"text": _DEMO, "max_sentences": 0}, {}):
        try:
            await summarize._text_summarize(CTX, args)
        except BusinessError as exc:
            assert exc.code == 1001
        else:
            raise AssertionError(f"非法入参应被 1001 拒绝：{args}")


async def test_normalize_collapses_whitespace_only() -> None:
    """格式统一：只动空白换行，正文一字不改。"""
    data = await summarize._text_normalize(CTX, {"text": "标题  \n\n\n正文第一行　\n正文第二行  "})
    assert data["normalized"] == "标题\n\n正文第一行\n正文第二行"
    assert data["changes"]["trailing_spaces_fixed"] == 2
    assert data["changes"]["blank_blocks_collapsed"] == 1


def test_summarize_registers_read_tools() -> None:
    """注册口径：读 scope、免审批。"""
    specs = {spec.name: spec for spec in summarize.specs()}
    assert set(specs) == {"office.text.summarize", "office.text.normalize"}
    assert all(spec.scope == "office:read" for spec in specs.values())
    assert all(spec.requires_approval is False for spec in specs.values())
