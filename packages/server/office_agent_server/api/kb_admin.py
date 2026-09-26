"""知识库后台端点（PRD §2.13：资料上传 / 知识库维护 / 入库统计 / 按源删除）

链路：POST /kb/files（multipart 上传 → 入盘 → 解析 → 切块 → 向量化）
      → GET /kb/files（来源清单）→ DELETE /kb/files/{name}（按源删除）
      → GET /kb/stats（入库统计）。
口径：管理面端点，全部经 require_any_perm("admin")——非管理面角色 403（前端如实提示，
      不降级给假数据）；写动作直接生效：管理员配置知识库即配置变更，不入业务库、
      不涉跨系统出站，故不走审批闸门（与 /jobs、/workflows 管理面同口径）。
      端点只做解析 + 调 service + ok()，业务与降级判定留在 services/tools-office。
对齐：AGENTS.md §3（端点薄封装 / 鉴权在壳层）；智能办公Agent 产品需求文档.md §2.13。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.rbac import CurrentUser, require_any_perm
from office_agent_server.responses import ok
from office_agent_server.services import kb_admin as kb_admin_service
from office_agent_server.services.kb_admin import MAX_UPLOAD_BYTES

router = APIRouter(prefix="/kb", tags=["kb-admin"])

#: 知识库后台属管理面：仅 admin 可见（通配 `*` 照常放行）
KB_ADMIN_PERMS = ("admin",)


@router.post("/files")
async def upload_file(
    file: UploadFile = File(...),
    visibility: str = Form("public"),
    user: CurrentUser = Depends(require_any_perm(*KB_ADMIN_PERMS)),
) -> dict[str, Any]:
    """上传资料并立即解析入库（支持 pdf / docx / xlsx / csv / txt / md）。"""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"文件过大（上限 {MAX_UPLOAD_BYTES} 字节）：请拆分后再上传",
        )
    result = await kb_admin_service.upload_and_ingest(
        file.filename or "", data, uploaded_by=user.username, visibility=visibility
    )
    return ok(result, "上传成功：已解析并入库")


@router.get("/files")
async def list_files(
    user: CurrentUser = Depends(require_any_perm(*KB_ADMIN_PERMS)),
) -> dict[str, Any]:
    """知识库来源清单 + 入库统计摘要（管理员维护台数据源）。"""
    _ = user
    return ok(await kb_admin_service.list_sources(), "获取成功")


@router.delete("/files/{name}")
async def delete_file(
    name: str,
    user: CurrentUser = Depends(require_any_perm(*KB_ADMIN_PERMS)),
) -> dict[str, Any]:
    """按源文件删除：删文件 + 清该来源全部向量块 + 清来源清单条目。"""
    _ = user
    return ok(await kb_admin_service.delete_source(name), "删除成功")


@router.get("/stats")
async def kb_stats(
    user: CurrentUser = Depends(require_any_perm(*KB_ADMIN_PERMS)),
) -> dict[str, Any]:
    """知识库入库统计（来源数 / 切片数 / 向量化口径 / 按格式分组）。"""
    _ = user
    return ok(await kb_admin_service.kb_stats(), "获取成功")
