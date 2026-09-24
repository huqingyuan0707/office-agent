"""本地文档工作目录路径守卫（tools-office 内各文件工具共用）。

职责：把入参文件名锁进 Settings.DOCS_DIR / Settings.KB_DIR——
      basename 化防穿越 + 拒绝路径分隔符与相对段 + 后缀白名单。
对齐：office_agent/tools_docs.py 同款口径（resolve 前缀校验，越界一律 400）；
      AGENTS.md §3（数据不出域红线在本地盘的对应实现）。
"""

from __future__ import annotations

import os
from pathlib import Path

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings

_IMG_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def resolve_under_docs(filename: str, suffixes: tuple[str, ...]) -> Path:
    """把文件名解析到 DOCS_DIR 内的绝对路径（basename 化 + 后缀白名单 + 越界拒绝）。"""
    raw = str(filename or "").strip()
    name = os.path.basename(raw)
    if not name:
        allowed = " / ".join(suffixes)
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 filename 不能为空：请传入文件名（仅支持 {allowed}）"
        )
    if name != raw or ".." in raw:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"文件名只能是不含路径的名称（拒绝：{raw}）")
    if not name.lower().endswith(suffixes):
        allowed = " / ".join(suffixes)
        raise BusinessError(ErrorCode.PARAM_INVALID, f"文件名必须以 {allowed} 结尾（当前：{name}）")
    root = Path(settings.DOCS_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / name).resolve()
    if root not in target.parents and target != root:
        raise BusinessError(ErrorCode.PARAM_INVALID, "文件名非法：不允许越界访问文档工作目录")
    return target


def image_suffixes() -> tuple[str, ...]:
    """图片后缀白名单（OCR 用）。"""
    return _IMG_SUFFIXES
