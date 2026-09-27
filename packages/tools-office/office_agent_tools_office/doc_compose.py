"""大模型成稿工具（office.doc.compose，对齐 .trae/documents/对话办理重构-大模型串联三场景.md §机制件 3）

职责：office.doc.compose（office:read，免审批——只产文本无落盘副作用）——把编排层
      传入的素材（各步骤真实出参，占位符 {steps[*].result} 注入）交给 LLM 撰写
      中文 markdown 文档；成稿后做数字溯源校验（正文数字必须能在素材里找到）。

降级口径（降级绝不 500）：LLM_PROVIDERS 未配置 / 出站失败 / 返回空文本 →
      素材原文直出（degraded=True + degrade_reason），绝不编造正文。

分层红线：tools-office 不 import runtime（runtime→tools 单向），故本模块自读
      os.environ 的 LLM_PROVIDERS（与 runtime planner/llm.py 同键同构、各自解析），
      httpx 出站仿 retrieval.py 读 EMBEDDING_* 的先例；凭据只走环境变量绝不入库。
对齐：AGENTS.md §3（工具实现纯函数、降级置 degraded 标记）。
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

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

#: LLM_PROVIDERS 环境变量名（与 runtime planner/llm.py 同键；写入 .env 需启动装载，
#: 见 office_agent_server.__main__.load_env_file）
_LLM_ENV_KEY = "LLM_PROVIDERS"

#: 出站默认超时（秒）：成稿是长输出任务，本地小模型实测比规划慢，放宽到 60s
_DEFAULT_TIMEOUT_SECONDS = 60.0

#: 素材 JSON 送 LLM 的截断上限（字符）——防顶爆小模型上下文（Ollama 8192）
_MAX_MATERIAL_CHARS = 12000

#: 数字提取口径（两侧同规则，日期会被拆成同片段 therefore 不误报）
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: 我方落的生成时间行（元数据）：时间戳数字不属于业务数据，不参与数字校验
_META_LINE = re.compile(r"^生成时间：.*$", re.MULTILINE)


class _ComposeError(Exception):
    """成稿出站失败（内部标记，handler 捕获后走降级，绝不 500）。"""


@dataclass(frozen=True)
class _ProviderCfg:
    """LLM profile 最小配置（仅内存；凭据只来自环境变量）。"""

    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    @property
    def endpoint(self) -> str:
        """chat.completions 完整 URL（base_url 以 /v1 结尾约定，与 runtime 同口径）。"""
        base = self.base_url.rstrip("/")
        return f"{base}/chat/completions"


def _provider_of(name: str) -> _ProviderCfg:
    """解析 LLM_PROVIDERS 取命名 profile；未配置/非法抛 _ComposeError（调用方降级）。"""
    raw = os.environ.get(_LLM_ENV_KEY, "").strip()
    if not raw:
        raise _ComposeError(f"环境变量 {_LLM_ENV_KEY} 未配置，无法调用大模型成稿")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _ComposeError(f"环境变量 {_LLM_ENV_KEY} 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get(name), dict):
        known = "、".join(sorted(payload)) if isinstance(payload, dict) else "（结构非法）"
        raise _ComposeError(f"LLM profile「{name}」未配置，可用：{known or '（空）'}")
    value = payload[name]
    base_url = str(value.get("base_url") or "").strip()
    model = str(value.get("model") or "").strip()
    if not base_url or not model:
        raise _ComposeError(f"LLM profile「{name}」缺少 base_url 或 model")
    return _ProviderCfg(
        base_url=base_url,
        model=model,
        api_key=str(value.get("api_key") or ""),
        timeout_seconds=float(value.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
    )


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _compose_via_llm(
    cfg: _ProviderCfg, title: str, instruction: str, material_json: str
) -> str:
    """出站 chat.completions 成稿：状态码分级 + 截断素材 + 抽文本；失败抛 _ComposeError。"""
    if len(material_json) > _MAX_MATERIAL_CHARS:
        material_json = material_json[:_MAX_MATERIAL_CHARS] + "…（已截断）"
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是办公文档撰写助手。只依据给定素材 JSON 撰写中文 markdown 文档；"
                "文中出现的每个数字必须直接来自素材 JSON 的原值，禁止推算、汇总或编造；"
                "素材里没有的信息不要写，宁可留白。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文档标题：{title}\n"
                f"成稿要求：{instruction or '按素材整理成结构清晰的中文文档'}\n"
                f"素材 JSON（唯一数据来源）：\n{material_json}\n"
                "请输出 markdown 正文（以一级标题开头，不要输出与正文无关的说明）。"
            ),
        },
    ]
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            resp = await client.post(
                cfg.endpoint,
                json={"model": cfg.model, "messages": messages},
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise _ComposeError(f"大模型成稿超时（>{cfg.timeout_seconds}s）") from exc
    except (httpx.HTTPError, OSError) as exc:
        raise _ComposeError(f"大模型不可达（{type(exc).__name__}）") from exc
    if resp.status_code == 401:
        raise _ComposeError("大模型返回 401（凭据错误或未授权）")
    if resp.status_code >= 400:
        detail = resp.text[:200].strip()
        raise _ComposeError(f"大模型返回 HTTP {resp.status_code}（请求非法）：{detail}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise _ComposeError("大模型返回非 JSON 响应") from exc
    content = _extract_content(data)
    if not content:
        raise _ComposeError("大模型未返回文本，无法成稿")
    return content


def _extract_content(data: dict[str, Any]) -> str:
    """抽 chat.completions 文本（结构异常给空串，由调用方收口）。"""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "").strip()


def _fallback_document(title: str, material: Any) -> str:
    """降级直出：素材原文排版（本地渲染无上下文限制，给全量 pretty JSON）。"""
    return "\n".join(
        [
            f"# {title}",
            "",
            f"生成时间：{_now_text()}（大模型不可用，素材原文直出，未做任何改写）",
            "",
            "## 素材原文（各步骤真实出参）",
            "",
            "```json",
            json.dumps(material, ensure_ascii=False, indent=2, default=str),
            "```",
        ]
    )


def _check_numbers(document: str, material_json: str) -> dict[str, Any]:
    """数字溯源校验：正文里的数字片段必须能在素材 JSON 里找到，找不到即列失信清单。

    只标注不拦截（与终答数值校验「标注失信，不删回答」同口径）；两侧同一正则，
    日期「2026-09-26」会被同样拆成 2026/09/26，不产生误报；我方落的生成时间行
    是元数据（时间戳非业务数据），校验前剔除。
    """
    body = _META_LINE.sub("", document)
    material_numbers = set(_NUMBER.findall(material_json))
    doc_numbers = set(_NUMBER.findall(body))
    unverified = sorted(num for num in doc_numbers if num not in material_numbers)
    return {
        "verified": not unverified,
        "unverified": unverified[:20],
        "total_in_doc": len(doc_numbers),
    }


async def _doc_compose(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.doc.compose：素材（真实出参）→ LLM 成稿 markdown → 数字校验标注。"""
    _ = ctx
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入文档标题")
    material = args.get("material")
    if not isinstance(material, (list, dict)) or not material:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            "参数 material 必须是非空数组或对象（编排层用 {steps[*].result} 注入真实出参）",
        )
    instruction = str(args.get("instruction") or "").strip()
    profile = str(args.get("profile") or "default").strip() or "default"
    material_json = json.dumps(material, ensure_ascii=False, default=str)

    composed_by = "llm"
    degraded = False
    model = ""
    degrade_reason = ""
    try:
        cfg = _provider_of(profile)
        document = await _compose_via_llm(cfg, title, instruction, material_json)
        model = cfg.model
    except _ComposeError as exc:
        logger.warning("office.doc.compose 降级素材直出：%s", str(exc)[:200])
        document = _fallback_document(title, material)
        composed_by = "fallback"
        degraded = True
        degrade_reason = str(exc)[:200]

    return {
        "title": title,
        "document": document,
        "composed_by": composed_by,
        "degraded": degraded,
        "model": model,
        "degrade_reason": degrade_reason,
        "numbers_check": _check_numbers(document, material_json),
        "generated_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """大模型成稿工具的 ToolSpec（读口径，免审批：只产文本无落盘副作用）。

    timeout_seconds=150：成稿是长输出任务（LLM 出站默认 60s，素材截断后仍慢），
    executor 默认 30s 会掐死任务；max_retries=0：LLM 失败在工具内部已降级素材直出，
    外层重试只会把延迟放大 N 倍（2026-09-26 冒烟实测 30s×4 次把 POST /runs 拖超时）。
    """
    return (
        ToolSpec(
            name="office.doc.compose",
            scope=SCOPE_READ,
            timeout_seconds=150.0,
            max_retries=0,
            description=(
                "大模型成稿：把素材（各步骤真实出参，编排层经 {steps[*].result} 注入）"
                "交给大模型撰写中文 markdown 文档，成稿后做数字溯源校验；"
                "大模型不可用时降级素材原文直出（degraded 标记），绝不编造正文"
            ),
            params={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "description": "文档标题"},
                    "material": {
                        "description": "成稿素材：各步骤真实出参（数组或对象，唯一数据来源）"
                    },
                    "instruction": {
                        "type": "string",
                        "description": "成稿要求（可选，如：写成周报，含本周亮点与下周计划）",
                    },
                    "profile": {
                        "type": "string",
                        "description": "LLM profile 名（可选，默认 default）",
                    },
                },
                "required": ["title", "material"],
                "additionalProperties": False,
            },
            handler=_doc_compose,
        ),
    )


def register_all() -> list[str]:
    """注册大模型成稿工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
