"""评测 CLI 入口（``python -m office_agent_runtime.eval <dataset.yaml>``）

链路：解析参数（数据集 / plugins / db / json / md 输出路径）→ asyncio.run(run_eval)
      → 渲染报告落盘或打屏 → 按通过率返回退出码（0 = 全部通过，1 = 有未过或空集）。

口径：退出码语义同常见测试工具（CI 可据此接警告/门禁）；报告缺省打屏，
      显式给了 --json/--md 才落盘（不往仓库里偷偷写文件）；
      tools-office 未安装时示例数据集会缺工具——CLI 尝试注册、缺失只告警不阻断
      （可选依赖口径，仿 pptx_gen 依赖缺失不注册）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from office_agent_core.errors import BusinessError
from office_agent_runtime.eval.harness import run_eval
from office_agent_runtime.eval.report import render_json, render_markdown

logger = logging.getLogger(__name__)


def _register_office_tools() -> None:
    """尝试注册内置办公工具（tools-office 未安装只告警——可选依赖）。"""
    try:
        from office_agent_tools_office import register_all
    except ImportError:
        logger.warning("office_agent_tools_office 未安装，示例数据集所需工具可能缺席")
        return
    register_all()


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口（argv=None 时取 sys.argv[1:]；返回进程退出码）。"""
    parser = argparse.ArgumentParser(
        prog="office_agent_runtime.eval",
        description="Agent 评测 harness（数据集驱动，确定性 scorer 离线可跑）",
    )
    parser.add_argument("dataset", type=Path, help="评测数据集 YAML 路径")
    parser.add_argument(
        "--plugins", type=Path, default=None, help="智能体 plugins 目录（默认 loader.agents_dir()）"
    )
    parser.add_argument("--db", type=Path, default=None, help="评测库路径（默认临时库，跑完即弃）")
    parser.add_argument("--json", type=Path, default=None, help="JSON 报告输出路径（缺省打屏）")
    parser.add_argument("--md", type=Path, default=None, help="Markdown 报告输出路径（缺省打屏）")
    args = parser.parse_args(argv)

    try:
        report = asyncio.run(
            run_eval(
                args.dataset,
                plugins_dir=args.plugins,
                db_path=args.db,
                tool_registrar=_register_office_tools,
            )
        )
    except BusinessError as exc:
        print(f"评测失败：{exc.msg}", file=sys.stderr)
        return 2

    if args.json is not None:
        args.json.write_text(render_json(report), encoding="utf-8")
        print(f"JSON 报告已写入：{args.json}")
    if args.md is not None:
        args.md.write_text(render_markdown(report), encoding="utf-8")
        print(f"Markdown 报告已写入：{args.md}")
    if args.json is None and args.md is None:
        print(render_markdown(report))
    if report.total == 0:
        print("评测数据集为空，未执行任何用例", file=sys.stderr)
        return 1
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
