"""office.doc.compose 单测：LLM 成稿 / 降级素材直出 / 数字溯源校验（MockTransport 全程不出网）

链路：_provider_of 解析 LLM_PROVIDERS → _compose_via_llm 出站成稿 → handler 汇成
      composed_by/degraded/numbers_check 出参；LLM 不可用降级素材原文直出绝不 500。

对齐：.trae/documents/对话办理重构-大模型串联三场景.md §机制件 3；
      skills/doubao-coding-develop-unit-tests/SKILL.md。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from office_agent_tools_office import doc_compose


def _material() -> list[dict[str, Any]]:
    """成稿素材（与编排层 {steps[*].result} 注入形状一致）。"""
    return [
        {"index": 0, "tool": "office.schedule.view", "result": {"count": 3, "date": "2026-09-26"}}
    ]


def _compose(args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(doc_compose._doc_compose(None, args))  # type: ignore[arg-type]


# ---------------- ① 入参校验：title / material 缺失中文报错 ----------------


def test_compose_rejects_missing_title_and_material():
    with pytest.raises(Exception) as excinfo:
        _compose({"title": "", "material": _material()})
    assert "title" in str(excinfo.value)
    with pytest.raises(Exception) as excinfo:
        _compose({"title": "日报", "material": []})
    assert "material" in str(excinfo.value)


# ---------------- ② 降级：LLM_PROVIDERS 未配置 → 素材原文直出（degraded） ----------------


def test_compose_degrades_to_material_when_llm_unconfigured(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    out = _compose({"title": "工作日报", "material": _material()})
    assert out["composed_by"] == "fallback"
    assert out["degraded"] is True
    assert "素材原文" in out["document"]
    assert "LLM_PROVIDERS 未配置" in out["degrade_reason"]
    # 素材直出的数字必然可溯源
    assert out["numbers_check"]["verified"] is True


# ---------------- ③ LLM 成稿：composed_by=llm + 数字可溯源 ----------------


def test_compose_llm_path_marks_source_and_model(monkeypatch):
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    async def fake_compose(cfg, title, instruction, material_json):
        assert cfg.model == "fake-model"
        return f"# {title}\n\n今日共 3 场安排"

    monkeypatch.setattr(doc_compose, "_compose_via_llm", fake_compose)
    out = _compose({"title": "工作日报", "material": _material()})
    assert out["composed_by"] == "llm"
    assert out["degraded"] is False
    assert out["model"] == "fake-model"
    assert out["numbers_check"]["verified"] is True  # 3 来自素材


def test_compose_flags_untraceable_numbers(monkeypatch):
    """成稿里的数字不在素材里 → 失信清单标注（只标注不拦截）。"""
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )

    async def fake_compose(cfg, title, instruction, material_json):
        return "# 标题\n\n今日共 999 场安排"  # 999 不在素材（3/2026/09/26）

    monkeypatch.setattr(doc_compose, "_compose_via_llm", fake_compose)
    out = _compose({"title": "工作日报", "material": _material()})
    assert out["numbers_check"]["verified"] is False
    assert "999" in out["numbers_check"]["unverified"]


# ---------------- ④ 出站：请求体带素材与要求 / 401 降级不 500 ----------------


def test_compose_via_llm_payload_and_401_degrade(monkeypatch):
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps({"default": {"base_url": "http://llm.test/v1", "model": "fake-model"}}),
    )
    seen: dict[str, Any] = {}
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        body = json.loads(request.content)
        seen["content"] = body["messages"][-1]["content"]
        if state["n"] == 1:
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "# 成稿"}}]}
            )
        return httpx.Response(401, json={"error": "bad key"})

    # doc_compose 内部自建 AsyncClient：patch 模块级工厂换 MockTransport（monkeypatch 自动还原）；
    # lambda 内须调 patch 前捕获的真实类，否则递归调到已被 patch 的同名属性
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        doc_compose.httpx,
        "AsyncClient",
        lambda timeout=None: real_client(transport=httpx.MockTransport(handler)),
    )

    cfg = doc_compose._provider_of("default")
    material_json = json.dumps(_material(), ensure_ascii=False)
    out = asyncio.run(doc_compose._compose_via_llm(cfg, "工作日报", "写成日报", material_json))
    assert out == "# 成稿"
    # 出站请求体：素材 JSON 与成稿要求都注入 user 消息
    assert "office.schedule.view" in seen["content"]
    assert "写成日报" in seen["content"]

    # 401 → _ComposeError；handler 层捕获后降级素材直出（degrade_reason 带凭据口径）
    with pytest.raises(doc_compose._ComposeError) as excinfo:
        asyncio.run(doc_compose._compose_via_llm(cfg, "工作日报", "", material_json))
    assert "401" in str(excinfo.value)
    result = _compose({"title": "工作日报", "material": _material()})
    assert result["degraded"] is True
    assert "401" in result["degrade_reason"]


# ---------------- ⑤ _provider_of 解析：非法 JSON / 缺 profile / 缺字段 ----------------


def test_provider_of_parse_errors(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDERS", "not-json")
    with pytest.raises(doc_compose._ComposeError) as excinfo:
        doc_compose._provider_of("default")
    assert "不是合法 JSON" in str(excinfo.value)

    monkeypatch.setenv(
        "LLM_PROVIDERS", json.dumps({"other": {"base_url": "http://x/v1", "model": "m"}})
    )
    with pytest.raises(doc_compose._ComposeError) as excinfo:
        doc_compose._provider_of("default")
    assert "未配置" in str(excinfo.value)

    monkeypatch.setenv("LLM_PROVIDERS", json.dumps({"default": {"base_url": "", "model": "m"}}))
    with pytest.raises(doc_compose._ComposeError) as excinfo:
        doc_compose._provider_of("default")
    assert "缺少 base_url 或 model" in str(excinfo.value)
