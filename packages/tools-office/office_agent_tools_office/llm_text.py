"""大模型出站客户端（tools-office 内部唯一实现：解析 LLM_PROVIDERS + chat.completions 取文本）。

职责：
- provider_of()：解析环境变量 ``LLM_PROVIDERS`` 取命名 profile（凭据只在内存，绝不入库）；
- chat_text()：OpenAI 兼容 ``chat.completions`` 出站并取回纯文本
  （状态码分级 + 超时收口 + 非 JSON 收口 + 空文本收口）；
- check_numbers()：数字溯源校验（正文数字必须能在来源文本里找到）——成稿/润色/翻译共用。

为什么独立成模块：``office.doc.compose``（成稿）与 ``office.text.rewrite`` / ``office.text.translate``
（润色 / 整句翻译）共用同一出站链路与同一套降级口径——**同一行为只允许一个实现**；
tools-office 不 import runtime（依赖方向 runtime→tools 单向），故本模块自读环境变量
（与 ``runtime/planner/llm.py`` 同键同构、各自解析），httpx 出站仿 ``retrieval.py`` 读 EMBEDDING_* 的先例。

红线：出站失败只抛 ``LlmTextError``，由调用方降级（原文直出 + degraded 标记）——
      **绝不 500、绝不编造正文**；凭据只走环境变量，绝不入库、绝不落日志。
对齐：AGENTS.md §3（分层红线/降级绝不 500）；智能办公Agent 产品需求文档.md §2.1。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: LLM_PROVIDERS 环境变量名（与 runtime planner/llm.py 同键；写入 .env 需启动装载，
#: 见 office_agent_server.__main__.load_env_file）
LLM_ENV_KEY = "LLM_PROVIDERS"

#: 出站默认超时（秒）：长输出任务（成稿/翻译）本地小模型实测比规划慢
DEFAULT_TIMEOUT_SECONDS = 60.0

#: 数字提取口径（来源与正文同规则，日期会被拆成同片段因此不误报）
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: 我方落的生成时间行（元数据）：时间戳数字不属于业务数据，不参与数字校验
META_LINE = re.compile(r"^生成时间：.*$", re.MULTILINE)


class LlmTextError(Exception):
    """大模型出站失败（内部标记，handler 捕获后走降级，绝不 500）。"""


@dataclass(frozen=True)
class ProviderCfg:
    """LLM profile 最小配置（仅内存；凭据只来自环境变量）。"""

    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def endpoint(self) -> str:
        """chat.completions 完整 URL（base_url 以 /v1 结尾约定，与 runtime 同口径）。"""
        base = self.base_url.rstrip("/")
        return f"{base}/chat/completions"


def now_text() -> str:
    """显式 UTC 时间戳（溯源用，可跨时区比对）。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def provider_of(name: str) -> ProviderCfg:
    """解析 LLM_PROVIDERS 取命名 profile；未配置/非法抛 LlmTextError（调用方降级）。"""
    raw = os.environ.get(LLM_ENV_KEY, "").strip()
    if not raw:
        raise LlmTextError(f"环境变量 {LLM_ENV_KEY} 未配置，无法调用大模型")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmTextError(f"环境变量 {LLM_ENV_KEY} 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get(name), dict):
        known = "、".join(sorted(payload)) if isinstance(payload, dict) else "（结构非法）"
        raise LlmTextError(f"LLM profile「{name}」未配置，可用：{known or '（空）'}")
    value = payload[name]
    base_url = str(value.get("base_url") or "").strip()
    model = str(value.get("model") or "").strip()
    if not base_url or not model:
        raise LlmTextError(f"LLM profile「{name}」缺少 base_url 或 model")
    return ProviderCfg(
        base_url=base_url,
        model=model,
        api_key=str(value.get("api_key") or ""),
        timeout_seconds=float(value.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
    )


def extract_content(data: dict[str, Any]) -> str:
    """抽 chat.completions 文本（结构异常给空串，由调用方收口）。"""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "").strip()


async def chat_text(cfg: ProviderCfg, system: str, user: str) -> str:
    """出站 chat.completions 取文本；失败抛 LlmTextError（调用方降级，绝不 500）。"""
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            resp = await client.post(
                cfg.endpoint,
                json={
                    "model": cfg.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise LlmTextError(f"大模型超时（>{cfg.timeout_seconds}s）") from exc
    except (httpx.HTTPError, OSError) as exc:
        raise LlmTextError(f"大模型不可达（{type(exc).__name__}）") from exc
    if resp.status_code == 401:
        raise LlmTextError("大模型返回 401（凭据错误或未授权）")
    if resp.status_code >= 400:
        detail = resp.text[:200].strip()
        raise LlmTextError(f"大模型返回 HTTP {resp.status_code}（请求非法）：{detail}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise LlmTextError("大模型返回非 JSON 响应") from exc
    content = extract_content(data) if isinstance(data, dict) else ""
    if not content:
        raise LlmTextError("大模型未返回文本")
    return content


def check_numbers(text: str, source: str) -> dict[str, Any]:
    """数字溯源校验：正文里的数字片段必须能在来源文本里找到，找不到即列失信清单。

    只标注不拦截（与终答数值校验「标注失信，不删回答」同口径）；两侧同一正则，
    日期「2026-09-26」会被同样拆成 2026/09/26，不产生误报；我方落的生成时间行
    是元数据（时间戳非业务数据），校验前剔除。
    """
    body = META_LINE.sub("", text)
    source_numbers = set(_NUMBER.findall(source))
    doc_numbers = set(_NUMBER.findall(body))
    unverified = sorted(num for num in doc_numbers if num not in source_numbers)
    return {
        "verified": not unverified,
        "unverified": unverified[:20],
        "total_in_doc": len(doc_numbers),
    }
