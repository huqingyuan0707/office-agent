"""tools-office 测试公共夹具：把宿主环境钉进沙箱（防本机 data/ 残留污染用例）。

背景（2026-09-26 全量验证撞出）：本机 ``data/docs/data/*.csv`` 与
``data/knowledge/*`` 有真实运行/演示残留文件，台账装载器（data_analysis 的
CSV 叠加）与 kb.load_entries 会把它们叠进「只期待 builtin 条目」的断言
（sales 4→20 行、source 变 builtin-demo+local-csv、kb top1 变 local-kb:*）。

口径：autouse 把 DOCS_DIR 钉到逐用例空沙箱——需要真实落盘/读盘的用例
在自身内再 monkeypatch 覆盖（tmp_path 优先级高于本夹具），零改动照常工作。
KB_DIR/EMBEDDING/MILVUS/REDIS 的钉法在 test_kb_ask.py 各自夹具（口径注释在彼处）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.settings import settings


@pytest.fixture(autouse=True)
def _docs_dir_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOCS_DIR 钉到逐用例空目录：台账 CSV 叠加/事务存储/文档读写全不触真实 data/。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _pin_business_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉死业务时基：工作台账按 completed_at 聚合，跨真实日期的固定窗口用例才稳定。"""
    from datetime import datetime

    monkeypatch.setattr(
        "office_agent_tools_office.affairs.business_now",
        lambda: datetime(2026, 9, 26, 9, 0),
    )
    monkeypatch.setattr(
        "office_agent_tools_office.affairs._now_text",
        lambda: "2026-09-26 09:00:00 UTC",
    )
    monkeypatch.setattr(
        "office_agent_tools_office.affairs_schedule._now_text",
        lambda: "2026-09-26 09:00:00 UTC",
    )
    monkeypatch.setattr(
        "office_agent_tools_office.affairs_schedule.business_now",
        lambda: datetime(2026, 9, 26, 9, 0),
    )
