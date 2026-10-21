"""office.text.rewrite / office.text.translate 单测（LLM 路径 + 降级 + 入参红线，MockTransport 不出网）。

覆盖：入参红线（空文本/超长/未知风格/未知语言）；LLM 未配置 → 原文直出（degraded）；
      LLM 路径标注 composed_by=llm 与 model；新增数字进失信清单（只标注不拦截）；
      出站 401 → 降级不 500。
对齐：AGENTS.md §5；智能办公Agent 产品需求文档.md §2.1、§5.4。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from office_agent_tools_office import llm_text, rewrite

ORIGINAL = "本周共推进 3 个需求，其中 2 个已上线，剩余 1 个下周交付。"


def _call(kind: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = rewrite._text_rewrite if kind == "rewrite" else rewrite._text_translate
    return asyncio.run(handler(None, args))  # type: ignore[arg-type]


# ---------------- ① 入参红线 ----------------


def test_rewrite_rejects_empty_text_and_unknown_style():
    with pytest.raises(Exception, match="text"):
        _call("rewrite", {"text": ""})
    with pytest.raises(Exception, match="style"):
        _call("rewrite", {"text": ORIGINAL, "style": "unknown-style"})


def test_translate_rejects_missing_or_unknown_language():
    with pytest.raises(Exception, match="target_lang"):
        _call("translate", {"text": ORIGINAL})
    with pytest.raises(Exception, match="target_lang"):
        _call("translate", {"text": ORIGINAL, "target_lang": "克林贡语"})


def test_text_length_cap_rejected():
    with pytest.raises(Exception, match="字符"):
        _call("rewrite", {"text": "长" * 9000})


# ---------------- ② 降级：LLM 未配置 → 原文直出 ----------------


def test_rewrite_degrades_to_original_when_llm_unconfigured(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    out = _call("rewrite", {"text": ORIGINAL, "style": "concise"})
    assert out["degraded"] is True
    assert out["composed_by"] == "fallback"
    assert out["text"] == ORIGINAL
    assert "LLM_PROVIDERS 未配置" in out["degrade_reason"]


def test_translate_degrades_to_original_when_llm_unconfigured(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    out = _call("translate", {"text": ORIGINAL, "target_lang": "en"})
    assert out["degraded"] is True
    assert out["text"] == ORIGINAL
    assert out["target_language"] == "English"


# ---------------- ③ LLM 路径：标注来源与模型 ----------------


def test_rewrite_llm_path_marks_source_and_model(monkeypatch):
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    async def fake_chat(cfg, system, user):
        assert cfg.model == "fake-model"
        return "本周推进 3 个需求，2 个已上线，1 个下周交付。"

    monkeypatch.setattr(rewrite, "chat_text", fake_chat)
    out = _call("rewrite", {"text": ORIGINAL, "style": "concise"})
    assert out["composed_by"] == "llm"
    assert out["degraded"] is False
    assert out["model"] == "fake-model"
    assert out["numbers_check"]["verified"] is True


def test_translate_llm_path_marks_language(monkeypatch):
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    async def fake_chat(cfg, system, user):
        return "We shipped 3 requirements this week, 2 are live and 1 ships next week."

    monkeypatch.setattr(rewrite, "chat_text", fake_chat)
    out = _call("translate", {"text": ORIGINAL, "target_lang": "英语"})
    assert out["composed_by"] == "llm"
    assert out["target_lang"] == "英语"
    assert out["target_language"] == "English"


def test_rewrite_flags_invented_numbers(monkeypatch):
    """改写引入原文没有的数字 → 失信清单标注（只标注不拦截）。"""
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    async def fake_chat(cfg, system, user):
        return "本周推进 999 个需求。"

    monkeypatch.setattr(rewrite, "chat_text", fake_chat)
    out = _call("rewrite", {"text": ORIGINAL, "style": "formal"})
    assert out["numbers_check"]["verified"] is False
    assert "999" in out["numbers_check"]["unverified"]


# ---------------- ④ 出站：401 降级不 500 ----------------


def test_outbound_401_degrades_without_500(monkeypatch):
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        llm_text.httpx,
        "AsyncClient",
        lambda timeout=None: real_client(transport=httpx.MockTransport(handler)),
    )
    out = _call("rewrite", {"text": ORIGINAL})
    assert out["degraded"] is True
    assert out["text"] == ORIGINAL
    assert "401" in out["degrade_reason"]
