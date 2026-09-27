"""种子数据端到端验证脚本（只读链路，不落库）。

职责：验证 scripts/seed_demo_data.py 补齐的数据能被对话读链路真实查回——
- kb.ask：知识库检索（考勤/报销/用车 等高频主题）；
- office.data.query：sales 台账按人/月汇总；
- office.file.ask：网盘文档（周报/值班表/用车记录）问答；
- 多类型网盘文档存在性：PDF/PPTX/PNG/HTML/JSON/YAML/ZIP 与 meta.json。

运行：.venv\\Scripts\\python.exe scripts/verify_seed_queries.py
退出码：0 全部通过；1 有失败项。
对齐：AGENTS.md §5（贴验证命令输出）；智能办公Agent 产品需求文档.md §2.1/§2.4/§2.6。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from office_agent_core.contracts import ToolContext
from office_agent_core.settings import settings
from office_agent_tools_office import data_analysis, file_ask, kb

_TENANT = "demo-tenant"


def _ctx(username: str = "admin") -> ToolContext:
    """构造最小 ToolContext（只读工具用 username/tenant）。"""
    return ToolContext(
        username=username,
        tenant=_TENANT,
        roles=["admin"],
        trace_id="verify-seed",
    )


def _check_netdisk_files(failures: list[str]) -> None:
    """多类型网盘文档与元数据落盘检查（PDF/PPTX/PNG/HTML/JSON/YAML/ZIP/meta）。"""
    docs_dir = Path(settings.DOCS_DIR)
    expected = [
        "proc_会议室设备采购合同_v1.0_20260927.pdf",
        "hr_产品培训课纲_v1.0_20260927.pptx",
        "office_Q4项目看板_v1.0_20260927.png",
        "office_月度服务通知模板_v1.1_20260927.html",
        "agent_会议默认配置_v1.0_20260927.json",
        "agent_工具权限矩阵_v2.1_20260927.yaml",
        "project_Q4文档包_v1.0_20260927.zip",
        "proc_会议室设备采购合同_v1.0_20260927.pdf.meta.json",
    ]
    for name in expected:
        if not (docs_dir / name).is_file():
            failures.append(f"多类型网盘文档缺失：{name}")
            continue
        print(f"[多类型网盘] {name} OK")


async def main() -> int:
    failures: list[str] = []

    # 1) 知识库检索：考勤/报销/用车 各查一次，要求命中对应主题
    kb_checks = [
        ("弹性到岗时段是几点到几点", "考勤"),
        ("差旅住宿一线城市每晚上限多少", "差旅"),
        ("公务用车使用前需要做什么", "用车"),
        ("供应商准入要经过哪三方会签", "供应商"),
    ]
    for query, expect in kb_checks:
        result = await kb._kb_ask(_ctx(), {"query": query})
        snippets = " ".join(
            str(item.get("snippet") or "") for item in (result.get("results") or [])
        )
        titles = " ".join(str(item.get("title") or "") for item in (result.get("results") or []))
        ok = expect in snippets or expect in titles or query[:4] in snippets
        print(f"[知识库] 「{query}」→ {'命中' if ok else '未命中'} ({snippets[:40]}…)")
        if not ok:
            failures.append(f"kb.ask 未命中「{expect}」：{query}")

    # 2) 数据查询：sales 台账应返回叠加行（18 行覆盖 6 人 × 3 月）
    data = await data_analysis._data_query(_ctx(), {"dataset": "sales", "limit": 50})
    rows = data.get("rows") or []
    total = data.get("total") or 0
    print(f"[数据查询] sales 总行数 → {total}（返回前 {len(rows)} 行）")
    if total < 18 or len(rows) < 18:
        failures.append("office.data.query 未读到 sales 叠加台账")

    # 3) 网盘文档问答：周报/值班表/用车记录 各取一篇（查询词贴近文件原文措辞）
    file_checks = [
        ("2026-09-25-项目周报.md", "客服知识库"),
        ("2026-09-值班表.md", "值班"),
        ("2026-09-用车记录.txt", "公务用车"),
    ]
    for filename, expect in file_checks:
        result = await file_ask._file_ask(
            _ctx(), {"filename": filename, "query": f"{expect}相关的段落摘录"}
        )
        snippets = " ".join(
            str(item.get("snippet") or "") for item in (result.get("answer_fragments") or [])
        )
        count = result.get("count") or 0
        ok = count > 0 and (expect in snippets or filename in snippets)
        print(
            f"[网盘问答] {filename} → {'命中' if ok else '未命中'}（{count} 片段：{snippets[:40]}…）"
        )
        if not ok:
            failures.append(f"file.ask 未命中「{expect}」：{filename}")

    # 4) 多类型网盘文档与元数据
    _check_netdisk_files(failures)

    if failures:
        print(f"\n共 {len(failures)} 项失败：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n全部通过：知识库/数据台账/网盘文档（含多类型）均可被对话链路查回。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
