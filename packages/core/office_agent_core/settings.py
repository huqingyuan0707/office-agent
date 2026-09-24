"""全局配置（可调全进 Settings，禁止硬编码 URL/密钥/阈值）

链路：.env / 环境变量 → Settings → 内核各模块只读。

领域无关红线：本文件不出现任何具体业务域的配置项；具体上游提供方一律经
``LINKAGE_PROVIDERS`` 声明（凭据只走环境变量，绝不入库、绝不落代码）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_JWT_SECRET = "dev-only-change-me-and-rotate-in-prod"
_DEFAULT_SEED_PASSWORD = "admin123"


def is_default_seed_password(password: str) -> bool:
    """是否为本地演示默认口令（种子警告与建号拒绝两处共用，禁止生产使用）。"""
    return password == _DEFAULT_SEED_PASSWORD


class ProviderConfig(BaseModel):
    """一个上游工具提供方的接入配置（凭据只经环境变量注入）。"""

    base_url: str
    token: SecretStr = SecretStr("")
    path: str = "/api/v1/agent-gateway/invoke"
    timeout_seconds: float = 10.0

    def as_env_dict(self) -> dict[str, Any]:
        """脱敏投影（日志/巡检可打印，token 一律不出现）。"""
        return {
            "base_url": self.base_url,
            "path": self.path,
            "timeout_seconds": self.timeout_seconds,
            "token_configured": bool(self.token.get_secret_value()),
        }


def parse_providers(raw: str) -> dict[str, ProviderConfig]:
    """解析 ``LINKAGE_PROVIDERS`` 的 JSON 文本（非法 JSON 直接抛错，绝不静默降级为空）。"""
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("LINKAGE_PROVIDERS 必须是 {provider_id: {...}} 形式的 JSON 对象")
    return {str(key): ProviderConfig(**value) for key, value in payload.items()}


class Settings(BaseSettings):
    """应用配置（.env 模式，字段名即环境变量名）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "office-agent"
    APP_VERSION: str = "0.1.0"
    ENV: str = "dev"
    DATABASE_URL: str = "sqlite+aiosqlite:///./office-agent.db"
    CORS_ORIGINS: list[str] = []

    # ---- 登录鉴权 ----
    JWT_SECRET: SecretStr = SecretStr(_DEFAULT_JWT_SECRET)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 8 * 3600
    PASSWORD_HASH_ITERATIONS: int = 100_000
    PASSWORD_SALT_BYTES: int = 16
    ROLES_SEPARATOR: str = ","

    # ---- 演示种子（生产必关）----
    SEED_ON_START: bool = True
    SEED_TENANT: str = "demo-tenant"
    SEED_USERNAME: str = "admin"
    SEED_PASSWORD: SecretStr = SecretStr(_DEFAULT_SEED_PASSWORD)
    # 角色即权限、也是 Scope 令牌：默认管理员用通配符，避免内核硬编码插件 Scope。
    SEED_ROLES: str = "*"

    # ---- 工具执行（超时 30s / 幂等重试 3 次 / 熔断阈值与冷却）----
    AGENT_TOOL_TIMEOUT_SECONDS: float = 30.0
    AGENT_TOOL_MAX_RETRIES: int = 3
    AGENT_TOOL_CIRCUIT_THRESHOLD: int = 3
    AGENT_TOOL_CIRCUIT_COOLDOWN_SECONDS: int = 60
    AGENT_TOOL_RETRY_BACKOFF_SECONDS: float = 0.2

    # ---- 可观测 ----
    OBSERVABILITY_ENABLED: bool = True
    OBSERVABILITY_DIR: str = "data/observability"

    # ---- 本地办公工作目录（文档对比 / 自定义模板 / 图片OCR / 知识库文件）----
    DOCS_DIR: str = "data/docs"
    KB_DIR: str = "data/knowledge"

    # ---- 主动消息推送（站内通知扫描阈值）----
    APPROVAL_STALE_HOURS: float = 24.0
    NOTIFICATION_MAX_PER_SCAN: int = 50

    # ---- 跨系统联动 ----
    # 键 = provider_id；值 = {base_url, path, token, timeout_seconds}。
    # 环境变量示例（JSON）：
    # LINKAGE_PROVIDERS={"ecommerce":{"base_url":"http://127.0.0.1:8100","token":"<jwt>"}}
    LINKAGE_PROVIDERS: dict[str, ProviderConfig] = {}
    # 出站标识头值前缀，上游据此识别调用方
    LINKAGE_CLIENT_NAME: str = "office-agent"

    @model_validator(mode="after")
    def _guard_prod(self) -> Settings:
        """生产红线：显式密钥 + 关种子（本地默认值绝不允许进生产）。"""
        if self.ENV != "prod":
            return self
        secret = self.JWT_SECRET.get_secret_value()
        if secret == _DEFAULT_JWT_SECRET or len(secret) < 32:
            raise ValueError("生产环境必须显式设置 JWT_SECRET 且不少于 32 字符")
        if self.SEED_ON_START:
            raise ValueError("生产环境必须设 SEED_ON_START=false，种子账号仅用于开发演示")
        return self


settings = Settings()
