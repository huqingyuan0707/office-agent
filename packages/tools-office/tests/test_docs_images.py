"""office.docs.extract_images 单测（批量导出容器内图片 + 入参红线 + 落盘位置守卫）。

覆盖：docx 容器内图片批量导出到 extracted/ 子目录；无图容器如实 0 张；
      非 docx/pptx 1001、缺文件 404、短幂等键 1001；路径穿越拒绝。
对齐：AGENTS.md §5；智能办公Agent 产品需求文档.md §2.1。
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Any

import pytest

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_tools_office import docs_images

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _call(args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(docs_images._docs_extract_images(None, args))  # type: ignore[arg-type]


def _make_docx(path: Path, media: dict[str, bytes]) -> None:
    """造一个最小 docx 容器（word/media/* 放图片）。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for name, data in media.items():
            archive.writestr(f"word/media/{name}", data)


@pytest.fixture()
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """文档工作目录钉到临时目录（每例独立）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


def test_extract_images_writes_files_to_extracted_dir(docs_dir: Path) -> None:
    """docx 内嵌图片批量导出到 extracted/<源名>/ 子目录。"""
    _make_docx(docs_dir / "方案.docx", {"image1.png": PNG, "image2.png": PNG})
    out = _call({"filename": "方案.docx", "idem_key": "extract-0001"})
    assert out["count"] == 2
    assert {item["name"] for item in out["extracted"]} == {"image1.png", "image2.png"}
    written = sorted((docs_dir / "extracted" / "方案").glob("*.png"))
    assert len(written) == 2
    assert all(item["bytes"] > 0 for item in out["extracted"])


def test_extract_images_replay_overwrites_same_files(docs_dir: Path) -> None:
    """重复导出覆盖同名文件（同内容同结果，不产生重复文件）。"""
    _make_docx(docs_dir / "方案.docx", {"image1.png": PNG})
    first = _call({"filename": "方案.docx", "idem_key": "extract-0002"})
    second = _call({"filename": "方案.docx", "idem_key": "extract-0002"})
    assert first["count"] == second["count"] == 1
    assert len(list((docs_dir / "extracted" / "方案").glob("*.png"))) == 1


def test_container_without_media_reports_zero(docs_dir: Path) -> None:
    """容器内无图如实 0 张（不编造）。"""
    _make_docx(docs_dir / "空.docx", {})
    out = _call({"filename": "空.docx", "idem_key": "extract-0003"})
    assert out["count"] == 0
    assert out["extracted"] == []


def test_bad_inputs_rejected(docs_dir: Path) -> None:
    """非 docx/pptx 1001、缺文件 404、短幂等键 1001、路径穿越拒绝。"""
    (docs_dir / "笔记.md").write_text("x", encoding="utf-8")
    with pytest.raises(BusinessError) as exc:
        _call({"filename": "笔记.md", "idem_key": "extract-0004"})
    assert exc.value.code == ErrorCode.PARAM_INVALID

    with pytest.raises(BusinessError) as exc:
        _call({"filename": "不存在.docx", "idem_key": "extract-0005"})
    assert exc.value.code == ErrorCode.NOT_FOUND

    with pytest.raises(BusinessError) as exc:
        _call({"filename": "方案.docx", "idem_key": "short"})
    assert exc.value.code == ErrorCode.PARAM_INVALID

    with pytest.raises(BusinessError) as exc:
        _call({"filename": "../逃逸.docx", "idem_key": "extract-0006"})
    assert exc.value.code == ErrorCode.PARAM_INVALID
