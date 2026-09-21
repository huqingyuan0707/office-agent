"""runtime 单测夹具：临时库 + 挂载 runtime 的宿主 app + 本地演示工具（全程不出网）

环境变量必须在导入 ``office_agent_core.settings`` 之前设置：Settings 在导入期实例化一次，
之后改环境不会再生效。agent.yaml 一律经 tmp_path + monkeypatch loader.agents_dir 提供，
不向仓库 plugins/ 写入任何文件。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_DB_FILE = Path(tempfile.gettempdir()) / "office-agent-runtime-test.db"
_DB_FILE.unlink(missing_ok=True)

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_FILE.as_posix()}"

#: 本地演示工具的入参 Schema（echo 类，幂等只读，零外部依赖）
_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "title": "内容", "minLength": 1}},
    "required": ["text"],
    "additionalProperties": False,
}


@pytest.fixture(scope="session")
def client():
    """带 lifespan 的测试客户端（mount 先于 startup：run_steps 随建表一起出来）。"""
    from fastapi.testclient import TestClient

    from office_agent_runtime.ext import mount
    from office_agent_server.app import create_app

    app = create_app()
    mount(app)  # 原地挂载（无返回值），先于 startup：run_steps 随建表一起出来
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _isolate_and_demo_tools():
    """逐用例复位熔断并注册本地演示工具（echo 类；重复注册为幂等覆盖）。"""
    from office_agent_core import executor, registry
    from office_agent_core.contracts import ToolContext, ToolSpec

    async def _echo(ctx: ToolContext, args: dict) -> dict:
        return {"echo": args, "by": ctx.username}

    async def _shout(_ctx: ToolContext, args: dict) -> dict:
        text = str(args.get("text", ""))
        return {"shout": text.upper(), "length": len(text)}

    for spec in (
        ToolSpec(
            name="demo.echo",
            scope="office:read",
            description="原样回传入参",
            params=_TEXT_SCHEMA,
            handler=_echo,
        ),
        ToolSpec(
            name="demo.shout",
            scope="office:read",
            description="转成大写并计长度",
            params=_TEXT_SCHEMA,
            handler=_shout,
        ),
    ):
        registry.register(spec)
    executor.reset_breakers()
    yield
    executor.reset_breakers()


_DEMO_YAML = """\
name: demo-assistant
description: 演示智能体：回声 → 大写 两步链路
system_prompt: |
  你是演示助手，只能使用白名单内的工具。
tools: [demo.echo, demo.shout, demo.boom]
max_steps: 4
rules:
  - match: ["演示", "回声"]
    steps:
      - tool: demo.echo
        args: {text: "hello"}
      - tool: demo.shout
        args: {text: "{steps[0].result.echo.text}"}
"""

_CAPPED_YAML = """\
name: capped-agent
description: 步数上限演示：两步规则但 max_steps=1
tools: [demo.echo, demo.shout]
max_steps: 1
rules:
  - match: ["连跑"]
    steps:
      - tool: demo.echo
        args: {text: "a"}
      - tool: demo.shout
        args: {text: "{steps[0].result.echo.text}"}
"""


@pytest.fixture
def agent_yaml_dir(tmp_path, monkeypatch):
    """提供测试用 plugins 根目录并让 loader 指过去（不污染仓库 plugins/）。"""
    from office_agent_runtime import loader

    root = tmp_path / "plugins"
    for dirname, text in (("demo-assistant", _DEMO_YAML), ("capped-agent", _CAPPED_YAML)):
        target = root / dirname
        target.mkdir(parents=True)
        (target / "agent.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(loader, "agents_dir", lambda: root)
    return root
