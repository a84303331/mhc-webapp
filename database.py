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
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost:5432/mhc")

# 自動轉換 Railway 格式
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20, pool_recycle=3600)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency: 提供 async DB session"""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """初始化資料庫（建立所有 table）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
