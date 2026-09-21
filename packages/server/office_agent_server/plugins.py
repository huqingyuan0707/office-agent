"""插件工具装载器（扫描 plugins/*/plugin.py 并调用其 register()）

链路：app 工厂 lifespan → load_plugin_tools() → 各插件 register() 向注册中心注册工具。

红线：本模块是**通用装载器**，不认识任何具体业务域——插件目录按仓库布局推导，
      因此新增一个插件不需要改主包任何一行代码（主包零「if 某业务」分支）。
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

#: 插件入口文件名（约定：每个插件目录下放一个 plugin.py，暴露 register()）
PLUGIN_ENTRY = "plugin.py"


def plugins_dir() -> Path:
    """插件根目录（仓库布局：``<repo>/plugins``）。"""
    return Path(__file__).resolve().parents[3] / "plugins"


def plugin_paths() -> list[Path]:
    """全部插件入口（稳定排序，便于启动日志与断言）。"""
    root = plugins_dir()
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob(f"*/{PLUGIN_ENTRY}") if path.is_file())


def _module_name(path: Path) -> str:
    """由目录名生成合法模块名（目录名允许带连字符）。"""
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in path.parent.name)
    return f"office_agent_plugin_{safe}"


def _load_module(path: Path) -> ModuleType:
    """按文件路径装载插件模块（不依赖插件是否进了 sys.path）。"""
    spec = importlib.util.spec_from_file_location(_module_name(path), path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法装载插件：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin_tools() -> list[str]:
    """装载全部插件，返回成功装载的插件名。

    单个插件失败只告警不阻断启动：一个插件的环境没配好，
    不应让整个办公 Agent 起不来（未装载的工具在清单里自然缺席）。
    """
    loaded: list[str] = []
    for path in plugin_paths():
        name = path.parent.name
        try:
            module = _load_module(path)
            register = getattr(module, "register", None)
            if not callable(register):
                logger.warning("插件 %s 未暴露 register()，已跳过", name)
                continue
            register()
        except Exception as exc:
            logger.warning("插件 %s 装载失败：%s", name, str(exc)[:200])
            continue
        loaded.append(name)
    return loaded