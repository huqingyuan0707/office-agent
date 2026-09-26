"""知识库后台壳层服务（转发 tools-office 的文档管理原语 + 装配守卫）。

链路：api/kb_admin → 本模块 → office_agent_tools_office.kb_admin
      （入盘 / 解析 / 切块 / 向量化 / 统计 / 按源删除）。
口径：壳层只做装配守卫（工具包未装如实报系统级不可用，不伪装成空清单）；
      业务判定与降级（解析引擎缺失 / 向量库不可用）全在工具包内，壳层不复制一份。
对齐：AGENTS.md §3（分层红线：端点薄封装 / 降级绝不 500）；PRD §2.13（知识库后台）。
"""

from __future__ import annotations

from typing import Any

from office_agent_core.errors import BusinessError, ErrorCode

try:
    # tools-office 是可选 pip 包（与 app.py / services/notifications.py 同一守卫口径）
    from office_agent_tools_office import kb_admin as _kb_admin
except ImportError:  # pragma: no cover - 取决于装配
    _kb_admin = None  # type: ignore[assignment]

_UNAVAILABLE = "办公工具包未安装（office-agent-tools-office）：知识库后台不可用"

#: 上传体积上限（唯一出处是 tools-office kb_admin.MAX_UPLOAD_BYTES；工具包未装时给同值
#: 兜底——此常量只用于端点前置拒绝超大请求，真正的解析判定在工具包内）
MAX_UPLOAD_BYTES = _kb_admin.MAX_UPLOAD_BYTES if _kb_admin is not None else 200_000


def _tools() -> Any:
    """取工具包模块；未装配时如实报系统级不可用。"""
    if _kb_admin is None:
        raise BusinessError(ErrorCode.INTERNAL, _UNAVAILABLE)
    return _kb_admin


async def upload_and_ingest(
    filename: str, data: bytes, *, uploaded_by: str, visibility: str
) -> dict[str, Any]:
    """上传入盘 + 解析 + 切块 + 向量化（后台「资料上传」）。"""
    return await _tools().save_and_ingest(
        filename, data, uploaded_by=uploaded_by, visibility=visibility
    )


async def list_sources() -> dict[str, Any]:
    """来源清单（文件实况 × 来源清单联查，两侧不一致如实标 state）。"""
    return await _tools().list_sources()


async def kb_stats() -> dict[str, Any]:
    """知识库统计（来源聚合 + 按格式分组 + 向量存储口径）。"""
    return await _tools().kb_stats()


async def delete_source(filename: str) -> dict[str, Any]:
    """按源删除（删文件 + 清该源向量块 + 清清单条目）。"""
    return await _tools().delete_source(filename)
