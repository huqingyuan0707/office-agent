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
def _isolate_breakers():
    """逐用例复位熔断：前序用例把某工具打到开闸，不该连坐后序用例（真实环境靠冷却恢复）。

    注意只复位熔断位，**不能** ``registry.reset()``——那会清掉 lifespan 注册的插件工具。
    """
    from office_agent_core import executor

    executor.reset_breakers()
    yield
    executor.reset_breakers()