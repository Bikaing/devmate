# backend/dependencies.py
# FastAPI 依赖注入：① 数据库会话 get_db  ② 当前用户鉴权 get_current_user
import asyncio
import sys
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config import get_settings

settings = get_settings()

# Windows 兼容：psycopg 异步跑不了 ProactorEventLoop，切 Selector（Linux/macOS 不生效）；
# 放在引擎创建前，保证任何导入本模块的异步链路都在 loop 创建前完成切换
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── 创建异步引擎（连接池）──
engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    echo=False,             # True 会打印所有 SQL，调试时可临时打开
)

# ── 会话工厂 ──
# expire_on_commit=False：commit 后对象不过期，接口里还能继续读属性
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：获取异步数据库会话，自动提交 / 回滚 / 关闭"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── JWT 鉴权 ──
bearer_scheme = HTTPBearer()  # 自动从请求头解析 "Authorization: Bearer <token>"，缺失/格式错直接 401


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """FastAPI 依赖：验证 JWT Token，返回当前用户信息。
    返回 {"user_id": str, "role": str}；Token 无效则抛 401。"""
    credentials_exception = HTTPException(  # 预先准备好「401 凭证无效」，多处复用
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # 用密钥和算法解码；签名不对/过期会抛 JWTError
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str = payload.get("sub")        # 标准字段 sub = 用户ID
        role: str = payload.get("role", "user")  # 角色，缺省普通用户
        if not user_id:                          # Token 里没有用户ID，视为无效
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    return {"user_id": user_id, "role": role}
