"""office.pptx.outline 单测（大纲生成 + 规则降级 + 入参红线，MockTransport 不出网）。

覆盖：入参红线（text/filename 二选一、文件不存在 404、页数上限）；LLM 路径产出
      与 office.pptx.generate 同形的 slides；LLM 未配置/返回坏 JSON → 规则分段降级
      （degraded）；数字只来自原文。
对齐：AGENTS.md §5；智能办公Agent 产品需求文档.md §2.12。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_tools_office import pptx_outline

REPORT = """# 本周工作汇报

- 完成数据看板联调，指标口径对齐 3 处
- 报销流程优化，平均审批时长从 5 天降到 2 天
- 下周推进移动端适配与灰度发布
"""


def _call(args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(pptx_outline._pptx_outline(None, args))  # type: ignore[arg-type]


def test_requires_text_or_filename():
    with pytest.raises(BusinessError) as exc:
        _call({})
    assert exc.value.code == ErrorCode.PARAM_INVALID
    with pytest.raises(BusinessError):
        _call({"text": "内容", "filename": "a.md"})


def test_missing_file_404(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    with pytest.raises(BusinessError) as exc:
        _call({"filename": "不存在.md"})
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_max_slides_out_of_range_rejected():
    with pytest.raises(BusinessError, match="max_slides"):
        _call({"text": REPORT, "max_slides": 99})


def test_rule_fallback_when_llm_unconfigured(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    out = _call({"text": REPORT, "max_slides": 5})
    assert out["degraded"] is True
    assert out["composed_by"] == "fallback"
    assert out["slide_count"] >= 1
    # 规则降级只搬运原句，不编造内容
    flat = json.dumps(out["slides"], ensure_ascii=False)
    assert "灰度发布" in flat
    assert out["numbers_check"]["verified"] is True


def test_llm_path_returns_slides_compatible_with_pptx_generate(monkeypatch):
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    async def fake_chat(cfg, system, user):
        return (
            "```json\n"
            '{"slides": [{"title": "本周进展", "bullets": ["数据看板联调", "报销流程优化"]}]}\n'
            "```"
        )

    monkeypatch.setattr(pptx_outline, "chat_text", fake_chat)
    out = _call({"text": REPORT, "title": "周报"})
    assert out["degraded"] is False
    assert out["composed_by"] == "llm"
    assert out["model"] == "fake-model"
    assert out["slide_count"] == 1
    slide = out["slides"][0]
    assert set(slide) == {"title", "bullets"}
    assert slide["title"] and slide["bullets"]


def test_llm_bad_json_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    async def fake_chat(cfg, system, user):
        return "这里没有 JSON"

    monkeypatch.setattr(pptx_outline, "chat_text", fake_chat)
    out = _call({"text": REPORT})
    assert out["degraded"] is True
    assert out["slide_count"] >= 1
