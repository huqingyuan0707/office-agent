"""mcp-bridge 单测夹具（假 MCP Server 本体见 ``mcp_stub``，避免跨包 conftest 撞名）。"""

from __future__ import annotations

import pytest

from office_agent_core import linkage


@pytest.fixture(autouse=True)
def _clean_linkage():
    """每个用例前后清空提供方注册表（进程内单例，防用例间串味）。"""
    linkage.reset()
    yield
    linkage.reset()
