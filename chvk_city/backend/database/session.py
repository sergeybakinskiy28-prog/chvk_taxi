from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from chvk_city.backend.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with async_session() as session:
        yield session


__all__ = ["async_session", "engine", "get_db"]
