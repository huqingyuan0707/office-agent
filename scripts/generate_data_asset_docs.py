"""数据资产目录文档生成器（自动化产出 docs/data-assets/ 文档套件）

职责：以 packages/server/office_agent_server/data_asset_catalog.py 的
      DATA_ASSET_CATALOG 为唯一数据源，生成一套可读的目录文档：
      - docs/data-assets/README.md           总览（分类统计 + 全量清单表）
      - docs/data-assets/<分类>.md           每分类一份（条目 + 流水号表格）
      - docs/data-assets/index.json          机器可读目录（前端/API 复用的落盘镜像）

口径：文档只登记目录，不编造字段级细节——每条目的「用途」来自
      data_asset_catalog.asset_summary()（分类职责 + 资产形态），
      具体内容由实现阶段细化，本脚本如实标注。
      文档带生成时间与水印，改目录后重跑本脚本即全量再生（幂等覆写）。

链路：python scripts/generate_data_asset_docs.py → 覆写 docs/data-assets/。
红线：不触 ORM/FastAPI/网络，纯本地文件生成；目录数据只认 data_asset_catalog，
      任何单元测试/文档都禁止在别处硬编码目录。
对齐：AGENTS.md §3（零硬编码：目录数据集中于此）；README 数据资产目录一节。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from office_agent_server.data_asset_catalog import (
    DATA_ASSET_CATALOG,
    asset_kind,
    catalog_items,
    category_intro,
)

_DOC_ROOT = Path(__file__).resolve().parents[1] / "docs" / "data-assets"


def _now() -> str:
    """生成时间戳（UTC，naive，与业务落库口径一致）。"""
    return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _watermark() -> str:
    """文档水印（声明该文档由脚本自动化生成，勿手改）。"""
    return (
        "> 本文件由 `scripts/generate_data_asset_docs.py` 自动化生成，"
        "**请勿手工编辑**；目录变更请改 `data_asset_catalog.py` 后重跑本脚本。"
    )


def render_category(category: str) -> str:
    """渲染一个分类的文档（页眉 + 条目流水表）。"""
    names = DATA_ASSET_CATALOG[category]
    lines = [
        f"# {category}",
        "",
        _watermark(),
        "",
        f"本类共登记 {len(names)} 条数据文档/数据资产。",
        "",
        category_intro(category),
        "",
        "| # | 资产名称 | 资产形态 | 用途摘要 |",
        "| --- | --- | --- | --- |",
    ]
    for idx, name in enumerate(names, start=1):
        summary = f"按「{asset_kind(name)}」登记，具体字段/内容由实现阶段细化。"
        lines.append(f"| {idx} | {name} | {asset_kind(name)} | {summary} |")
    lines.extend(["", f"生成时间：{_now()}。", ""])
    return "\n".join(lines)


def render_readme() -> str:
    """渲染总览（分类统计 + 全量清单索引）。"""
    total = len(catalog_items())
    stats = "\n".join(
        f"| {idx} | {cat} | {len(assets)} |"
        for idx, (cat, assets) in enumerate(DATA_ASSET_CATALOG.items(), start=1)
    )
    index_rows = "\n".join(
        f"| {idx} | [{cat}](./{cat}.md) | {len(assets)} |"
        for idx, (cat, assets) in enumerate(DATA_ASSET_CATALOG.items(), start=1)
    )
    return "\n".join(
        [
            "# 数据资产目录（数据文档/数据资产清单）",
            "",
            _watermark(),
            "",
            f"> 共 {len(DATA_ASSET_CATALOG)} 个分类、{total} 条数据资产登记（按分类语境，不全局去重）。",
            "",
            "## 分类统计",
            "",
            "| # | 分类 | 条目数 |",
            "| --- | --- | --- |",
            stats,
            "",
            "## 分类文档索引",
            "",
            "| # | 分类 | 条目数 |",
            "| --- | --- | --- |",
            index_rows,
            "",
            f"生成时间：{_now()}。",
            "",
        ]
    )


def render_index_json() -> str:
    """机器可读目录镜像（与 catalog_items 同序，供前端/API 复用）。"""
    return json.dumps(
        {"generated_at": _now(), "items": catalog_items()},
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    """按 README → 分类文档 → index.json 顺序全量覆写 docs/data-assets/。"""
    _DOC_ROOT.mkdir(parents=True, exist_ok=True)
    (_DOC_ROOT / "README.md").write_text(render_readme(), encoding="utf-8")
    for category in DATA_ASSET_CATALOG:
        (_DOC_ROOT / f"{category}.md").write_text(render_category(category), encoding="utf-8")
    (_DOC_ROOT / "index.json").write_text(render_index_json(), encoding="utf-8")
    print(
        f"已生成 {len(DATA_ASSET_CATALOG) + 2} 个文件到 {_DOC_ROOT} "
        f"（{len(catalog_items())} 条资产，幂等覆写）"
    )


if __name__ == "__main__":
    main()
