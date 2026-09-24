"""壳层单测夹具：临时库 + 假上游提供方（全程不打真实网络）

环境变量必须在导入 ``office_agent_core.settings`` 之前设置：Settings 在导入期实例化一次，
之后改环境不会再生效。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_DB_FILE = Path(tempfile.gettempdir()) / "office-agent-test.db"
_DB_FILE.unlink(missing_ok=True)

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_FILE.as_posix()}"
# 假上游：地址只在测试进程内被 MockTransport 顶替，永不出网
os.environ["LINKAGE_PROVIDERS"] = (
    '{"ecommerce": {"base_url": "http://up.test", "token": "test-token"}}'
)


@pytest.fixture(scope="session")
def client():
    """带 lifespan 的测试客户端（启动装配：建表 / 审计 sink / 种子 / 提供方 / 插件）。"""
    from fastapi.testclient import TestClient

    from office_agent_server.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _isolate_affairs_store(tmp_path, monkeypatch):
    """事务存储钉进临时目录 + 业务时基钉死周三：真实 DOCS_DIR 的冒烟残留与真实星期几
    （周五会触发周报提示信号）都不该影响壳层单测的确定性。需要特定时基的用例自行再 patch。"""
    from datetime import datetime

    from office_agent_core.settings import settings

    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    try:
        from office_agent_tools_office import affairs as aff_mod

        monkeypatch.setattr(aff_mod, "business_now", lambda: datetime(2027, 6, 2, 9, 0))
    except ImportError:  # pragma: no cover - tools-office 是可选装配
        pass


@pytest.fixture(autouse=True)
def _isolate_breakers():
    """逐用例复位熔断：前序用例把某工具打到开闸，不该连坐后序用例（真实环境靠冷却恢复）。

    注意只复位熔断位，**不能** ``registry.reset()``——那会清掉 lifespan 注册的插件工具。
    """
    from office_agent_core import executor

    executor.reset_breakers()
    yield
    executor.reset_breakers()
