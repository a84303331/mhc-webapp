"""MHC WebApp — 認證模組

JWT 簽發/驗證、密碼雜湊、註冊/登入邏輯。
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Depends, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, generate_token

# ── 設定 ─────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-random-64-chars")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)


# ── 密碼工具 ─────────────────────────────────────
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── 密碼強度驗證 ────────────────────────────────
def validate_password_strength(password: str) -> Optional[str]:
    """驗證密碼強度，失敗回傳錯誤訊息，成功回傳 None"""
    if len(password) < 8:
        return "密碼長度至少 8 字元"
    if not any(c.isupper() for c in password):
        return "密碼至少須包含一個大寫英文字母"
    if not any(c.isdigit() for c in password):
        return "密碼至少須包含一個數字"
    return None


# ── JWT ─────────────────────────────────────────
def create_access_token(user_id: int, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "email": email, "exp": expire, "type": "access"}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token() -> str:
    import uuid
    return uuid.uuid4().hex


def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


# ── Auth Dependency ─────────────────────────────
async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """從 Bearer token 或 Cookie 獲取當前使用者"""
    token = None

    # 優先從 Authorization header
    if credentials:
        token = credentials.credentials
    # 備選從 cookie
    elif request:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="請先登入")

    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登入已過期，請重新登入")

    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="使用者不存在")

    return user


async def get_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """確保目前使用者為管理員"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="僅管理員可存取")
    return current_user


# ── 認證 API ────────────────────────────────────
async def register_user(
    db: AsyncSession,
    name: str,
    email: str,
    password: str,
) -> User:
    """註冊新使用者"""
    # 檢查是否已註冊
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="註冊失敗，請確認輸入資料正確或稍後再試")

    # 密碼強度
    pw_error = validate_password_strength(password)
    if pw_error:
        raise HTTPException(status_code=400, detail=pw_error)

    # 建立使用者
    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        email_verify_token=generate_token(),
        email_verify_token_expires=datetime.utcnow() + timedelta(hours=24),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> Optional[User]:
    """驗證使用者登入，成功回傳 User，失敗回傳 None"""
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


async def verify_email(db: AsyncSession, token: str) -> bool:
    """驗證郵箱 token，成功回傳 True"""
    result = await db.execute(
        select(User).where(
            User.email_verify_token == token,
            User.email_verify_token_expires > datetime.utcnow(),
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        return False

    user.email_verified = True
    user.email_verify_token = None  # type: ignore
    user.email_verify_token_expires = None  # type: ignore
    await db.commit()
    return True
