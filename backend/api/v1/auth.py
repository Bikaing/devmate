# backend/api/v1/auth.py
# 登录认证接口：/login（签发 Token）、/register（注册即登录）与 /me（验证鉴权）
import asyncio
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.core.logger import get_logger
from backend.dependencies import get_current_user, get_db

# ── 兼容性补丁：passlib 1.7.4 要读 bcrypt.__about__.__version__，而 bcrypt>=4 删了它 ──
import bcrypt as _bcrypt_mod, types as _types
if not hasattr(_bcrypt_mod, "__about__"):
    _about = _types.SimpleNamespace(__version__=getattr(_bcrypt_mod, "__version__", "4.x"))
    _bcrypt_mod.__about__ = _about  # 注入假的 __about__，让 passlib 能探测到版本

router = APIRouter()
logger = get_logger(__name__)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")  # 密码哈希上下文


class LoginRequest(BaseModel):
    """登录请求体。"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class RegisterRequest(BaseModel):
    """注册请求体。"""
    username: str = Field(..., min_length=3, max_length=64, description="用户名")
    password: str = Field(..., min_length=6, max_length=128, description="密码")


# 用户名只允许字母/数字/下划线/短横线，避免奇怪字符进入唯一索引
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class TokenResponse(BaseModel):
    """登录成功的响应体。"""
    access_token: str          # JWT 令牌
    token_type: str = "bearer"  # 令牌类型，固定 bearer
    expires_in: int            # 有效期（秒）
    role: str                  # 用户角色
    user_id: str               # 用户ID


def _create_access_token(data: dict, expires_minutes: int) -> str:
    """把身份信息 + 过期时间打包，用密钥签名成 JWT 字符串。"""
    settings = get_settings()
    payload = data.copy()  # 拷一份，避免改到原字典
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload["exp"] = expire  # exp 是 JWT 标准的过期时间字段
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """用户登录，返回 JWT Access Token。"""
    settings = get_settings()

    # 参数化查询防注入；users 表只有 username 一个登录凭据列
    result = await db.execute(
        text(
            "SELECT id, password_hash, role, is_active "
            "FROM users WHERE username = :val LIMIT 1"
        ),
        {"val": req.username},
    )
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not row.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用，请联系管理员")

    # 密码校验是 CPU 密集型（~100ms），用线程池避免阻塞事件循环
    loop = asyncio.get_running_loop()
    password_ok = await loop.run_in_executor(
        None, pwd_context.verify, req.password, row.password_hash
    )
    if not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    # 记录本次登录时间（get_db 会在接口结束时统一 commit）
    await db.execute(
        text("UPDATE users SET last_login_at = NOW() WHERE id = :id"),
        {"id": row.id},
    )

    # 校验通过，签发 Token（把用户身份装进去；本项目无租户，只带 sub 和 role）
    token = _create_access_token(
        data={"sub": str(row.id), "role": row.role},
        expires_minutes=settings.jwt_access_token_expire_minutes,
    )
    logger.info("auth.login_success", user_id=str(row.id), role=row.role)

    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,  # 分钟转秒
        role=row.role,
        user_id=str(row.id),
    )


@router.post("/register", response_model=TokenResponse)
async def register(
    req: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """注册新用户并直接签发 Token（注册即登录，免二次输入）。
    新账号默认 role=user、is_active=TRUE。"""
    settings = get_settings()

    if not _USERNAME_RE.fullmatch(req.username):
        raise HTTPException(status_code=400, detail="用户名只能包含字母、数字、下划线或短横线")

    exists = (await db.execute(
        text("SELECT 1 FROM users WHERE username = :val LIMIT 1"),
        {"val": req.username},
    )).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    # 密码哈希 CPU 密集，放线程池避免阻塞事件循环
    loop = asyncio.get_running_loop()
    pwd_hash = await loop.run_in_executor(None, pwd_context.hash, req.password)

    user_id = (await db.execute(
        text("INSERT INTO users (username, password_hash, role) "
             "VALUES (:u, :h, 'user') RETURNING id"),
        {"u": req.username, "h": pwd_hash},
    )).scalar_one()
    logger.info("auth.register_success", user_id=str(user_id))

    token = _create_access_token(
        data={"sub": str(user_id), "role": "user"},
        expires_minutes=settings.jwt_access_token_expire_minutes,
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        role="user",
        user_id=str(user_id),
    )


@router.get("/me")
async def get_me(
    current_user: dict = Depends(get_current_user),  # 注入当前用户（顺带完成鉴权）
):
    """获取当前登录用户信息（用于验证 Token 是否有效）。"""
    return current_user


# ── 模块自测：验证密码哈希与 Token 编解码（不依赖数据库）──
if __name__ == "__main__":
    h = pwd_context.hash("Test@123456")
    print("正确密码校验:", pwd_context.verify("Test@123456", h))
    print("错误密码校验:", pwd_context.verify("wrong", h))

    from jose import jwt as _jwt
    s = get_settings()
    tk = _create_access_token({"sub": "u-1", "role": "user"}, s.jwt_access_token_expire_minutes)
    decoded = _jwt.decode(tk, s.jwt_secret_key, algorithms=[s.jwt_algorithm])
    print("解码出 sub:", decoded["sub"], "| role:", decoded["role"])
