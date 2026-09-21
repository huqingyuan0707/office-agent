"""认证层：账号登录（管理员 + 复核员，bcrypt 校验）→ 签发 JWT → get_current 统一鉴权依赖。

职责：
- authenticate()：校验 ADMIN_*/REVIEWER_* 种子账号（bcrypt checkpw；明文种子仅在首次使用时 hash 进内存，
  绝不落库、绝不入日志；复核员留空 = 单人演示模式）；
- create_access_token()：签发 HS256 JWT（sub=用户名，scopes=[office:read, office:write]，exp 按 JWT_EXPIRE_MINUTES）；
- get_current()：FastAPI 依赖。解析 Authorization: Bearer <JWT>，缺失/无效/过期一律 HTTP 401。
链路：POST /auth/login → authenticate + create_access_token；其余业务路由 Depends(get_current) → CurrentUser → executor Scope 校验。
对齐：docs/office-agent仓库骨架与内核提取方案.md §2（server 壳 auth）、§7.3（凭据只走环境变量，绝不入库）。

实现注记：passlib 1.7.4 与 bcrypt 5.x 不兼容（hash 直接抛 ValueError），故直接使用 bcrypt 库完成 hash/verify。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from office_agent.config import settings

ALGORITHM = "HS256"
DEFAULT_SCOPES = ["office:read", "office:write"]

_bearer = HTTPBearer(auto_error=False)
_seed_hashes: dict[str, bytes] = {}


@dataclass
class CurrentUser:
    """当前登录用户：用户名 + 授权范围（来自 JWT 载荷）。"""

    username: str
    scopes: list[str]


def _account_hashes() -> dict[str, bytes]:
    """首次登录时把明文种子口令 hash 进内存（懒初始化，进程内复用）。

    账号 = 管理员 + 复核员（REVIEWER_USERNAME 留空则不启用）——双人审批红线的第二身份。
    """
    global _seed_hashes
    if not _seed_hashes:
        accounts = {settings.ADMIN_USERNAME: settings.ADMIN_PASSWORD}
        if settings.REVIEWER_USERNAME:
            accounts[settings.REVIEWER_USERNAME] = settings.REVIEWER_PASSWORD
        _seed_hashes = {
            name: bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt())
            for name, pwd in accounts.items()
            if name
        }
    return _seed_hashes


def authenticate(username: str, password: str) -> CurrentUser | None:
    """校验用户名口令；通过返回 CurrentUser，失败返回 None（不区分用户名/口令错误细节，防枚举）。"""
    stored = _account_hashes().get(username)
    if stored is None:
        return None
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), stored)
    except ValueError:
        ok = False
    return CurrentUser(username=username, scopes=list(DEFAULT_SCOPES)) if ok else None


def create_access_token(user: CurrentUser) -> str:
    """签发 JWT（sub + scopes + iat + exp）。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.username,
        "scopes": user.scopes,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


async def get_current(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    """鉴权依赖：Bearer JWT 缺失/无效/过期一律 HTTP 401。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401, detail="未登录：缺少访问令牌，请先 POST /auth/login 获取"
        )
    try:
        payload = jwt.decode(credentials.credentials, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期，请重新登录") from None
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="访问令牌无效：缺少主体信息")
    return CurrentUser(username=str(username), scopes=[str(s) for s in payload.get("scopes") or []])
