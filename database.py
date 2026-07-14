"""MHC WebApp — 資料庫層

PostgreSQL async connection + SQLAlchemy models。
Railway 會自動注入 DATABASE_URL。
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from dotenv import load_dotenv

load_dotenv()

# Railway 自動注入 DATABASE_URL（格式：postgresql://...）
# 需轉換為 asyncpg 格式：postgresql+asyncpg://...
_RAW_DATABASE_URL = os.getenv("DATABASE_URL", "")

def _get_async_url() -> str:
    """延遲解析 DATABASE_URL，避免 import 時 crash"""
    url = _RAW_DATABASE_URL
    if not url:
        # 嘗試 Railway 參考變數（可能尚未解析）
        url = os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost:5432/mhc")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL not configured")
    return url

engine = None
async_session = None

def _init_engine():
    global engine, async_session
    if engine is None:
        engine = create_async_engine(
            _get_async_url(),
            echo=False,
            pool_size=10,
            max_overflow=20,
            pool_recycle=3600,
            connect_args={"timeout": 10, "command_timeout": 10},
        )
        async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency: 提供 async DB session"""
    _init_engine()
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """初始化資料庫（建立所有 table）"""
    _init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
