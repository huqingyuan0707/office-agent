"""启动入口：``python -m office_agent_server``

默认监听 127.0.0.1:8200（对齐前端默认 base）；生产请用显式 uvicorn 参数或反向代理。
"""

from __future__ import annotations

import uvicorn

#: 本地演示默认监听（生产以部署参数为准，不写死在代码逻辑里）
HOST = "127.0.0.1"
PORT = 8200


def main() -> None:
    """起服务（reload 只在本地演示有意义，故不默认开）。"""
    uvicorn.run("office_agent_server.app:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()