from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.config import Settings

class Database:
    """管理 PostgreSQL 异步连接池和事务会话。"""
    def __init__(self, settings: Settings) -> None:
        # 数据库连接中心，初始化连接池信息
        self.engine: AsyncEngine = create_async_engine(
            str(settings.database_url),
            pool_pre_ping=True,
            pool_size=20,
            max_overflow=20,
            pool_recycle=1800,
        )
        # Session 连接池
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )
    
    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """提供请求或 Worker 使用的事务会话。"""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    
    async def close(self) -> None:
        """服务关闭时释放数据库连接池。"""
        await self.engine.dispose()
