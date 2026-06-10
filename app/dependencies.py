"""
Shared FastAPI dependencies.

These are injected via `Depends(...)` in route handlers and bot middleware.

  - `get_db`    — yields an async SQLAlchemy session
  - `get_redis` — yields a Redis async connection, or None if REDIS_URL is unset
"""

from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# ---------------------------------------------------------------------------
# Database — async SQLAlchemy engine + session factory
# ---------------------------------------------------------------------------

_engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

_AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides a transactional AsyncSession.

    Usage::

        @router.get("/example")
        async def example(db: AsyncSession = Depends(get_db)):
            ...

    The session is automatically closed (and rolled back on unhandled errors)
    after the request completes.
    """
    async with _AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Redis — optional async connection
# ---------------------------------------------------------------------------

async def get_redis() -> AsyncGenerator[Optional[object], None]:
    """
    FastAPI dependency that provides an async Redis client.

    Returns ``None`` when ``REDIS_URL`` is not configured so that components
    can fall back gracefully to in-process state (e.g., the confirmation store).

    Usage::

        @router.post("/example")
        async def example(redis=Depends(get_redis)):
            if redis is not None:
                await redis.set("key", "value")

    The connection is closed after the request completes.
    """
    if not settings.redis_url:
        yield None
        return

    try:
        import redis.asyncio as aioredis  # type: ignore[import]
    except ImportError:
        # redis package not installed — treat as unavailable
        yield None
        return

    client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        yield client
    finally:
        await client.aclose()
