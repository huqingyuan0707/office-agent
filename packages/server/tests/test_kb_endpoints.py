"""知识库后台端点单测（HTTP 级：multipart 上传 → 解析入库 → 清单/统计 → 按源删除）

覆盖：admin 门槛（无 token 401 / viewer 403）；上传即入库并在清单与统计可见；
      kb.ask 能立刻检索到刚上传的资料（口径贯通）；非白名单后缀 / 超限 / 未知源删除如实报错。
固件隔离：逐用例把 KB_DIR 钉进 tmp_path，并钉空 EMBEDDING_*/MILVUS_URI/REDIS_URL（零网络）。
对齐：AGENTS.md §3（端点薄封装 / 降级绝不 500）、§5（验证命令）；
      智能办公Agent 产品需求文档.md §2.13（知识库后台）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core import kv
from office_agent_core.settings import settings
from office_agent_server.db import session_factory
from office_agent_server.models import User
from office_agent_server.security import hash_password
from office_agent_tools_office import vector_store

_DOC = "远程办公制度\n每周最多 2 天远程办公，需提前一天在系统报备。"


def _login(client, username: str = "admin", password: str = "admin123") -> dict[str, str]:  # type: ignore[no-untyped-def]
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


async def _ensure_viewer() -> None:
    """补一个 viewer 角色用户（种子只有 admin/reviewer；reviewer 含 admin 故不能用来验 403）。"""
    from sqlalchemy import select

    async with session_factory()() as session:
        exists = (
            await session.execute(
                select(User).where(User.tenant == "demo-tenant", User.username == "kbdoc-viewer")
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                User(
                    tenant="demo-tenant",
                    username="kbdoc-viewer",
                    pwd_hash=hash_password("viewer123"),
                    roles="viewer",
                    status="active",
                )
            )
            await session.commit()


@pytest.fixture(autouse=True)
def _isolate_kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """知识库目录钉进临时目录 + 外部向量通道钉空：用例不写真实 data/knowledge、不发网络。"""
    monkeypatch.setattr(settings, "KB_DIR", str(tmp_path / "kb"))
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "MILVUS_URI", "")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    vector_store.reset_store()
    kv.reset()
    yield
    vector_store.reset_store()
    kv.reset()


def _upload(client, name: str, payload: bytes, visibility: str = "public", **kwargs):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/kb/files",
        files={"file": (name, payload, "application/octet-stream")},
        data={"visibility": visibility},
        **kwargs,
    )


# ---------------- ① 鉴权门槛 ----------------


@pytest.mark.asyncio
async def test_kb_endpoints_require_admin(client) -> None:  # type: ignore[no-untyped-def]
    """无 token 401；viewer 403（无假数据可退，前端如实提示权限不足）。"""
    assert client.get("/api/v1/kb/files").status_code == 401
    assert client.get("/api/v1/kb/stats").status_code == 401

    await _ensure_viewer()
    viewer = _login(client, "kbdoc-viewer", "viewer123")
    assert client.get("/api/v1/kb/files", headers=viewer).status_code == 403
    assert client.get("/api/v1/kb/stats", headers=viewer).status_code == 403
    assert _upload(client, "a.md", _DOC.encode(), headers=viewer).status_code == 403

    assert client.get("/api/v1/kb/files", headers=_login(client)).status_code == 200


# ---------------- ② 上传 → 清单 → 统计 → 删除 全链 ----------------


def test_upload_list_stats_delete_roundtrip(client) -> None:  # type: ignore[no-untyped-def]
    """上传即解析入库：清单标「已入库」、统计按格式分组、删除后清空（全走真实接口）。"""
    auth = _login(client)

    resp = _upload(client, "远程办公制度.md", _DOC.encode("utf-8"), "hr", headers=auth)
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    uploaded = body["data"]
    assert uploaded["filename"] == "远程办公制度.md"
    assert uploaded["format"] == "md"
    assert uploaded["visibility"] == "hr"
    assert uploaded["chunks"] == 1
    assert uploaded["degraded"] is False
    assert uploaded["reuploaded"] is False
    assert uploaded["vector_mode"] == "unconfigured"  # 未配向量检索：如实标注，不谎称已向量化

    listing = client.get("/api/v1/kb/files", headers=auth).json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["state"] == "indexed"
    assert listing["items"][0]["visibility"] == "hr"
    assert listing["summary"]["files"] == 1
    assert listing["summary"]["chunks"] == 1
    assert set(listing["supported_formats"]) == {"pdf", "docx", "xlsx", "csv", "txt", "md"}
    assert listing["source"] == "local-kb-admin" and listing["fetched_at"]

    stats = client.get("/api/v1/kb/stats", headers=auth).json()["data"]
    assert stats["summary"]["files"] == 1
    assert stats["by_format"] == [
        {"format": "md", "files": 1, "chunks": 1, "bytes": len(_DOC.encode("utf-8"))}
    ]
    assert stats["store"]["mode"] == "unconfigured"

    removed = client.delete("/api/v1/kb/files/远程办公制度.md", headers=auth).json()
    assert removed["code"] == 0 and removed["data"]["deleted"] is True
    assert client.get("/api/v1/kb/files", headers=auth).json()["data"]["total"] == 0
    assert client.get("/api/v1/kb/stats", headers=auth).json()["data"]["summary"]["files"] == 0


def test_uploaded_file_is_immediately_retrievable_by_kb_ask(client) -> None:  # type: ignore[no-untyped-def]
    """上传即可被 kb.ask 检索到：后台入盘与问答同一提取/切块/来源口径，无需额外注册。"""
    auth = _login(client)
    assert (
        _upload(client, "远程办公制度.md", _DOC.encode("utf-8"), headers=auth).json()["code"] == 0
    )

    resp = client.post(
        "/api/v1/agent/tools/kb.ask/invoke",
        json={"args": {"query": "远程办公每周最多几天", "top_k": 1}},
        headers=auth,
    )
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    result = body["data"]["result"]
    assert result["results"][0]["source"] == "local-kb:远程办公制度.md"
    assert "2 天" in result["results"][0]["snippet"]


def test_reupload_same_name_reports_replaced(client) -> None:  # type: ignore[no-untyped-def]
    """重传同名文件：清单不重复计数，回执如实标 reuploaded（旧向量块先清再入库）。"""
    auth = _login(client)
    _upload(client, "制度.md", _DOC.encode("utf-8"), headers=auth)
    second = _upload(
        client, "制度.md", "远程办公制度（已修订）\n每周最多 3 天。".encode(), headers=auth
    ).json()["data"]
    assert second["reuploaded"] is True
    assert client.get("/api/v1/kb/files", headers=auth).json()["data"]["total"] == 1


# ---------------- ③ 如实报错（不 500、不静默） ----------------


def test_upload_rejects_bad_format_and_oversize(client) -> None:  # type: ignore[no-untyped-def]
    """非白名单后缀 1001；超出上限 1001（与解析上限同一出处，不落「已上传未入库」的坑）。"""
    auth = _login(client)
    bad_suffix = _upload(client, "evil.exe", b"MZ", headers=auth).json()
    assert bad_suffix["code"] == 1001, bad_suffix

    from office_agent_server.services.kb_admin import MAX_UPLOAD_BYTES

    oversize = _upload(client, "big.md", b"x" * (MAX_UPLOAD_BYTES + 1), headers=auth).json()
    assert oversize["code"] == 1001, oversize
    assert client.get("/api/v1/kb/files", headers=auth).json()["data"]["total"] == 0


def test_upload_rejects_traversal_and_bad_visibility(client) -> None:  # type: ignore[no-untyped-def]
    """路径穿越与非法可见范围一律 1001（文件名锁知识库目录）。"""
    auth = _login(client)
    assert _upload(client, "../evil.md", b"x", headers=auth).json()["code"] == 1001
    assert _upload(client, "a.md", b"x", "has space", headers=auth).json()["code"] == 1001


def test_delete_unknown_source_is_404(client) -> None:  # type: ignore[no-untyped-def]
    """删除不存在的来源如实 404（前端按码提示，不假装删成功）。"""
    resp = client.delete("/api/v1/kb/files/nope.md", headers=_login(client))
    assert resp.status_code == 404
    assert resp.json()["code"] == 1004
