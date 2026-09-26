"""知识库来源清单与磁盘原语（PRD §2.13 知识库后台的存储层）。

职责（纯本地实现，不触 ORM / FastAPI）：
- KB_DIR 定位与惰性创建、来源标识 source_key（与 kb.ask 的 source 约定同一出处，
  按源删除才删得对）、来源清单 .kb_manifest.json 的读 / 写 / 单条 patch；
- 可见范围口径校验 visibility_or_raise（public 或角色名；非法即 1001）；
- 目录内可入库文件清点 list_kb_files、文本提取 extract_text（复用 file_read
  pdf/docx/xlsx/csv/txt/md 同一提取口径）、标题摘要 title_from_text。

链路：kb_admin（上传/解析/删除/统计编排）→ 本模块 → file_read。
红线：清单是元数据，损坏/缺失只告警重置，绝不阻断入库；同步 IO 一律由调用方包
      asyncio.to_thread（本模块导出的同步函数不在内部转线程）。
对齐：AGENTS.md §3（分层红线 / 降级绝不 500 / 零硬编码）；
      智能办公Agent 产品需求文档.md §2.13（知识库后台）、§2.6（知识库增强检索）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_tools_office import file_read

logger = logging.getLogger(__name__)

#: 可入库格式（与 office.file.read 同一白名单，避免两处口径漂移）
KB_SUFFIXES = file_read.READ_SUFFIXES
#: 向量与条目的来源域（与 kb.load_entries / retrieval.retrieve 的 origin 约定一致）
ORIGIN = "knowledge"
#: 来源清单文件名（KB_DIR 内隐藏文件；后缀 .json 不在 KB_SUFFIXES 内，不会被当资料读）
MANIFEST_NAME = ".kb_manifest.json"
#: 摘要标题上限（防超长首行把列表撑爆）
MAX_TITLE_CHARS = 80
#: 可见范围取值上限（public 或角色名）
_MAX_VISIBILITY_CHARS = 32

#: 清单读改写串行锁（进程内；跨进程并发部署不在本仓当前口径内）
STORE_LOCK = asyncio.Lock()


def kb_root() -> Path:
    """知识库目录（惰性创建）。"""
    root = Path(settings.KB_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def source_key(filename: str) -> str:
    """来源标识：与 kb.load_entries 生成的 source 完全一致（按源删除才删得对）。"""
    return f"local-kb:{filename}"


def manifest_path() -> Path:
    """来源清单绝对路径。"""
    return kb_root() / MANIFEST_NAME


def utc_now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_manifest() -> dict[str, dict[str, Any]]:
    """读来源清单：缺失/损坏一律返回空表并告警——清单是元数据，坏了不该阻断入库。"""
    path = manifest_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("知识库来源清单损坏已重置：%s", str(exc)[:120])
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def write_manifest(entries: dict[str, dict[str, Any]]) -> None:
    """整体覆写清单（调用方持锁，避免并发读改写互相覆盖）。"""
    manifest_path().write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


async def patch_manifest(filename: str, patch: dict[str, Any] | None) -> None:
    """更新（patch=None 时移除）某来源的清单条目（进程内串行）。"""
    async with STORE_LOCK:
        entries = await asyncio.to_thread(load_manifest)
        if patch is None:
            entries.pop(filename, None)
        else:
            entries[filename] = {**entries.get(filename, {}), **patch}
        await asyncio.to_thread(write_manifest, entries)


def manifest_visibility() -> dict[str, str]:
    """清单里的「文件名 → 可见范围」映射（kb.load_entries 一次读取后逐文件过滤）。"""
    return {
        name: str(entry.get("visibility") or "public") for name, entry in load_manifest().items()
    }


def visibility_or_raise(raw: Any) -> str:
    """可见范围口径：缺省 public；非空即角色名（不含空白，长度受限）。"""
    value = str(raw or "").strip() or "public"
    if len(value) > _MAX_VISIBILITY_CHARS or any(ch.isspace() for ch in value):
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            "可见范围只能是不含空白的 1-32 字符（public 或角色名，如 hr）",
        )
    return value


def list_kb_files(root: Path) -> dict[str, int]:
    """目录内可入库文件 → 体积映射（同步 IO，调用方包 to_thread）。"""
    if not root.is_dir():
        return {}
    try:
        return {
            path.name: path.stat().st_size
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in KB_SUFFIXES
        }
    except OSError as exc:
        logger.warning("知识库目录不可读：%s", str(exc)[:120])
        return {}


async def extract_text(path: Path) -> dict[str, Any]:
    """提取文本：引擎缺失/文件损坏由 extract_document 转 degraded，异常也不外抛。"""
    try:
        return await asyncio.to_thread(file_read.extract_document, path)
    except (OSError, ValueError) as exc:
        logger.warning("知识库解析失败 %s：%s", path.name, str(exc)[:120])
        return {
            "text": "",
            "truncated": False,
            "degraded": True,
            "degraded_reason": f"解析失败：{str(exc)[:120]}",
        }


def title_from_text(text: str, fallback: str) -> str:
    """标题取正文首个非空行（去 markdown 修饰）；取不到退回文件名，不编造。"""
    first = next((line.strip("# \t") for line in text.splitlines() if line.strip()), "")
    return (first[:MAX_TITLE_CHARS] or fallback)[:MAX_TITLE_CHARS]
