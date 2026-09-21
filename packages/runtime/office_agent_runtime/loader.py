"""智能体配置装载器（扫描 plugins/*/agent.yaml）

链路：api 清单/受理时调 load_agent_specs() → yaml 解析 → parse_agent_spec 校验。

红线：与 server 的 load_plugin_tools() 同路径纪律——目录不存在返回空列表；单文件
     解析/校验失败只 logger.warning 跳过，绝不拖垮进程（一份配置写坏，
     不该让整个智能体运行时起不来，其余智能体照常可用）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.spec import AgentSpec, parse_agent_spec

logger = logging.getLogger(__name__)

#: 智能体配置文件名（约定：每个插件目录下最多一份 agent.yaml）
AGENT_CONFIG = "agent.yaml"


def agents_dir() -> Path:
    """智能体配置根目录（仓库布局：``<repo>/plugins``；测试用 monkeypatch 替换本函数）。"""
    return Path(__file__).resolve().parents[3] / "plugins"


def agent_config_paths() -> list[Path]:
    """全部智能体配置路径（稳定排序，便于启动日志与断言）。"""
    root = agents_dir()
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob(f"*/{AGENT_CONFIG}") if path.is_file())


def load_agent_specs() -> list[AgentSpec]:
    """装载全部智能体声明；单文件失败只告警跳过（口径见模块 docstring）。"""
    specs: list[AgentSpec] = []
    for path in agent_config_paths():
        name = path.parent.name
        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
            specs.append(parse_agent_spec(raw if isinstance(raw, dict) else {}))
        except Exception as exc:  # 解析/校验失败都算「这份配置坏」，跳过即可
            logger.warning("智能体配置 %s 装载失败：%s", name, str(exc)[:200])
            continue
    return specs


def find_agent_spec(name: str) -> AgentSpec:
    """按名取智能体声明；取不到 1004（中文提示已装载清单，便于自查）。"""
    for spec in load_agent_specs():
        if spec.name == name:
            return spec
    known = "、".join(spec.name for spec in load_agent_specs()) or "（暂无）"
    raise BusinessError(ErrorCode.NOT_FOUND, f"智能体不存在：{name}，已装载：{known}", 404)
