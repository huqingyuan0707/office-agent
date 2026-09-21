"""应用配置（pydantic-settings：环境变量 + .env 双读，全仓唯一取值口径）。

职责：集中声明全部可调项——JWT/管理员种子/自有库连接/外部系统桥接开关/工具超时；
其余模块一律 `from office_agent.config import settings` 取值，禁止散落硬编码。
链路：进程启动 → Settings() 读环境变量与 .env → db/executor/auth/tools_* 各模块引用。
对齐：docs/office-agent仓库骨架与内核提取方案.md §6（零硬编码 Settings/.env 模式平迁）、§7.5（可选插件红线）。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 认证 ---
    # JWT 签名密钥：默认仅兜底开发演示，生产环境必须用环境变量/.env 覆盖
    JWT_SECRET: str = "dev-secret-change-me"
    JWT_EXPIRE_MINUTES: int = 720
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    # 复核员账号（可选）：双人审批红线的第二身份；留空 = 单人演示模式（审批闭环走不通正向批准）
    REVIEWER_USERNAME: str = "reviewer"
    REVIEWER_PASSWORD: str = "reviewer123"
    # 文档工具工作目录：office.docx/xlsx/pptx 读写都锁在此目录内（防路径穿越）
    DOCS_DIR: str = "data/docs"

    # --- 自有存储（与外部系统零连接，数据不出域） ---
    OFFICE_DB_URL: str = "sqlite+aiosqlite:///./office.db"

    # --- 可选外部系统桥接（领域桥接语义唯一出处见 tools_ecommerce.py，默认关闭） ---
    ECOMMERCE_ENABLED: bool = False
    ECOMMERCE_API_BASE: str = "http://127.0.0.1:8000"
    ECOMMERCE_TOKEN: str = ""

    # --- 工具执行 ---
    TOOL_TIMEOUT_SECONDS: int = 30


settings = Settings()
