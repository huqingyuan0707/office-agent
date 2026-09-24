"""启动入口：``python -m office_agent_server``

默认监听 127.0.0.1:8200（对齐前端默认 base）；生产请用显式 uvicorn 参数或反向代理。

可选装配智能体运行时：装了 office_agent_runtime 且环境变量 ``RUNTIME_ENABLED != "false"``
时挂载 /api/v1/agents 与 /api/v1/runs；没装则纯地基模式（行为与不装 runtime 完全一致）。
装配必须在 uvicorn 拿到 app 实例之前完成——RunStep 表挂在共享 metadata 上，
晚于 lifespan 建表才 import 会导致 run_steps 缺表。

启动先载入 .env（见 load_env_file）：``LLM_PROVIDERS``（LLM 规划器读 os.environ）与
``REVIEWER_USERNAME/PASSWORD``（种子读 os.environ）是直接读进程环境的键，
Settings 的 env_file 只填配置模型、不写 os.environ——不载入它们就是死配置。
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import uvicorn
from dotenv import load_dotenv

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: 本地演示默认监听（生产以部署参数为准，不写死在代码逻辑里）
HOST = "127.0.0.1"
PORT = 8200

#: runtime 开关环境变量（默认启用；设为 "false" 回到纯地基模式）
RUNTIME_ENABLED_ENV = "RUNTIME_ENABLED"


def create_runtime_app() -> FastAPI:
    """构造宿主应用并按开关可选挂载 runtime 扩展（mount 须先于 startup）。"""
    from office_agent_server.app import create_app

    app = create_app()
    if os.environ.get(RUNTIME_ENABLED_ENV, "true").strip().lower() == "false":
        logger.info("%s=false，智能体运行时未启用（纯地基模式）", RUNTIME_ENABLED_ENV)
        return app
    try:
        from office_agent_runtime.ext import mount
    except ImportError:
        logger.info("未安装 office_agent_runtime，以纯地基模式启动")
        return app
    mount(app)
    logger.info("智能体运行时已挂载：/api/v1/agents、/api/v1/runs")
    return app


def load_env_file() -> None:
    """把 .env 载入 os.environ（override=False：真实环境变量优先，部署可 env 覆盖）。

    只给「直接读 os.environ」的键兜底——Settings 侧由 pydantic-settings 的 env_file 自理，
    两边读到同一份 .env，不产生第二套配置源。
    在 __main__ 里做而不放 app 模块：测试导入的是 office_agent_server.app，
    载入进程环境是「起服务」的行为，不该污染用例环境。
    """
    load_dotenv(override=False)


def main() -> None:
    """起服务（reload 只在本地演示有意义，故不默认开）。"""
    load_env_file()
    uvicorn.run(create_runtime_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
