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
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .llm_text import LlmTextError as _ComposeError
from .llm_text import ProviderCfg as _ProviderCfg
from .llm_text import chat_text as _chat_text
from .llm_text import check_numbers as _check_numbers_impl
from .llm_text import now_text as _now_text
from .llm_text import provider_of as _provider_of

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

#: LLM_PROVIDERS 环境变量名（与 runtime planner/llm.py 同键；写入 .env 需启动装载，
#: 见 office_agent_server.__main__.load_env_file）
_LLM_ENV_KEY = "LLM_PROVIDERS"

#: 出站默认超时（秒）：成稿是长输出任务，本地小模型实测比规划慢，放宽到 60s
_DEFAULT_TIMEOUT_SECONDS = 60.0

#: 素材 JSON 送 LLM 的截断上限（字符）——防顶爆小模型上下文（Ollama 8192）
_MAX_MATERIAL_CHARS = 12000


async def _compose_via_llm(
    cfg: _ProviderCfg, title: str, instruction: str, material_json: str
) -> str:
    """出站 chat.completions 成稿：截断素材 + 组织提示词 + 取文本；失败抛 _ComposeError。

    出站链路本身在 llm_text.chat_text（与润色/翻译共用同一实现），本函数只负责
    成稿专属的提示词与素材截断。
    """
    if len(material_json) > _MAX_MATERIAL_CHARS:
        material_json = material_json[:_MAX_MATERIAL_CHARS] + "…（已截断）"
    system = (
        "你是办公文档撰写助手。只依据给定素材 JSON 撰写中文 markdown 文档；"
        "文中出现的每个数字必须直接来自素材 JSON 的原值，禁止推算、汇总或编造；"
        "素材里没有的信息不要写，宁可留白。"
    )
    user = (
        f"文档标题：{title}\n"
        f"成稿要求：{instruction or '按素材整理成结构清晰的中文文档'}\n"
        f"素材 JSON（唯一数据来源）：\n{material_json}\n"
        "请输出 markdown 正文（以一级标题开头，不要输出与正文无关的说明）。"
    )
    return await _chat_text(cfg, system, user)


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
    """数字溯源校验（实现已收口到 llm_text.check_numbers，此处保留同名薄封装）。

    只标注不拦截（与终答数值校验「标注失信，不删回答」同口径）。
    """
    return _check_numbers_impl(document, material_json)


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
