"""自定义模板工具（office.template.save / office.template.apply，对齐 PRD §2.1）。

职责：
- office.template.save（office:write + 恒送审 + 幂等键必带）：把周报/请假说明等模板
  （标题 + 若干可含 {占位符} 的段落）落盘为 DOCS_DIR/templates/{name}.json，
  审批通过才真正写盘（与 docx.write 同一套双人闭环）；
- office.template.apply（office:read）：读取模板并按入参 values 填充 {占位符}；
  未提供的占位符保持原样并在 unfilled 里如实列出（不编造填充值）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler；
磁盘 IO 走 asyncio.to_thread（不阻塞事件循环）。
红线：模板名经 basename 守卫锁进 templates 目录，绝不越界；读写响应带溯源标注。
对齐：AGENTS.md §3（分层红线/写动作恒送审）；智能办公Agent 产品需求文档.md §5.1（V1.0 自定义模板）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

_MAX_SECTIONS = 20
_PLACEHOLDER = re.compile(r"\{([^{}]{1,50})\}")
_NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff-]{1,64}$", re.UNICODE)


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _templates_dir() -> Path:
    """模板目录（DOCS_DIR/templates；惰性创建）。"""
    root = Path(settings.DOCS_DIR).resolve() / "templates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _template_path(name: str) -> Path:
    """校验模板名并解析到 templates 目录内的绝对路径（拒绝路径分隔符/越界）。"""
    cleaned = str(name or "").strip()
    if not cleaned or not _NAME_PATTERN.match(cleaned) or ".." in cleaned:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            "模板名只能由中文/字母/数字/下划线/连字符组成（1-64 字符，不含路径分隔符）",
        )
    root = _templates_dir()
    target = (root / f"{cleaned}.json").resolve()
    if root not in target.parents:
        raise BusinessError(ErrorCode.PARAM_INVALID, "模板名非法：不允许越界访问模板目录")
    return target


def _validate_sections(sections: Any) -> list[str]:
    """段落校验：非空字符串数组（1-20 条，每条 1-500 字符，可含 {占位符}）。"""
    if not isinstance(sections, list) or not sections:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 sections 必须为非空字符串数组（模板正文段落）"
        )
    if len(sections) > _MAX_SECTIONS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"模板段落最多 {_MAX_SECTIONS} 条（当前 {len(sections)} 条）"
        )
    cleaned = [str(item).strip() for item in sections]
    if any(not item or len(item) > 500 for item in cleaned):
        raise BusinessError(ErrorCode.PARAM_INVALID, "模板每条段落必须为 1-500 字符的非空文本")
    return cleaned


def _render(text: str, values: dict[str, Any]) -> tuple[str, list[str]]:
    """填充 {占位符}：values 给了就替换，没给保持原样并记入未填充清单。"""
    unfilled: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values and values[key] is not None:
            return str(values[key])
        unfilled.append(key)
        return match.group(0)

    return _PLACEHOLDER.sub(_sub, text), unfilled


async def _template_save(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.template.save：模板 JSON 落盘（审批通过后才被 decide 触发）。"""
    _ = ctx
    name = str(args.get("name") or "").strip()
    title = str(args.get("title") or "").strip()
    sections = _validate_sections(args.get("sections"))
    description = str(args.get("description") or "").strip()
    idem_key = str(args.get("idem_key") or "").strip()
    path = _template_path(name)

    payload = {
        "name": name,
        "title": title,
        "sections": sections,
        "description": description,
        "saved_at": _now_text(),
        "idem_key": idem_key,
    }

    def _write() -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    await asyncio.to_thread(_write)
    return {
        "name": name,
        "file": f"templates/{path.name}",
        "title": title,
        "section_count": len(sections),
        "published": True,
        "published_at": _now_text(),
        "note": "模板已保存到文档工作目录 templates/ 下；复用请调用 office.template.apply",
    }


async def _template_apply(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.template.apply：读取模板并填充占位符（缺值保持原样并如实列出）。"""
    _ = ctx
    path = _template_path(str(args.get("name") or ""))
    if not path.exists():
        raise BusinessError(
            ErrorCode.NOT_FOUND, f"模板不存在：{path.stem}（可用 office.template.save 先保存）", 404
        )
    values_raw = args.get("values")
    values: dict[str, Any] = values_raw if isinstance(values_raw, dict) else {}

    def _read() -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    payload = await asyncio.to_thread(_read)
    if not isinstance(payload, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, f"模板文件损坏（非 JSON 对象）：{path.name}")

    title, unfilled_title = _render(str(payload.get("title") or ""), values)
    rendered: list[str] = []
    unfilled: list[str] = list(unfilled_title)
    for section in payload.get("sections") or []:
        text, missing = _render(str(section), values)
        rendered.append(text)
        unfilled.extend(missing)

    return {
        "name": str(payload.get("name") or path.stem),
        "title": title,
        "sections": rendered,
        "unfilled": sorted(set(unfilled)),
        "source": f"local-docs:templates/{path.name}",
        "extracted_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """自定义模板两工具的 ToolSpec。"""
    return (
        ToolSpec(
            name="office.template.save",
            scope=SCOPE_WRITE,
            description="保存自定义模板（写动作）：恒送审 + idem_key 必填，审批通过后落盘 DOCS_DIR/templates/{name}.json；段落可含 {占位符} 供复用时填充",
            params={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "模板名（中文/字母/数字/下划线/连字符，1-64 字符）",
                        "minLength": 1,
                        "maxLength": 64,
                    },
                    "title": {
                        "type": "string",
                        "description": "模板标题（可含 {占位符}）",
                        "maxLength": 100,
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": '模板段落数组（1-20 条，每条可含 {占位符}，如 "本周完成：{本周工作}"）',
                    },
                    "description": {
                        "type": "string",
                        "description": "模板用途说明",
                        "maxLength": 200,
                    },
                    "idem_key": {
                        "type": "string",
                        "description": "幂等键（8-64 字符）",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                },
                "required": ["name", "sections", "idem_key"],
                "additionalProperties": False,
            },
            handler=_template_save,
            idempotent=True,
            requires_approval=True,
            approval_action="office.template.save",
        ),
        ToolSpec(
            name="office.template.apply",
            scope=SCOPE_READ,
            description="复用自定义模板：读取模板并按 values 填充 {占位符}；未提供的占位符保持原样并在 unfilled 如实列出（不编造填充值）",
            params={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "模板名",
                        "minLength": 1,
                        "maxLength": 64,
                    },
                    "values": {
                        "type": "object",
                        "description": '占位符取值，形如 {"本周工作": "..."}（缺省只回模板骨架）',
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=_template_apply,
        ),
    )


def register_all() -> list[str]:
    """注册自定义模板工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
