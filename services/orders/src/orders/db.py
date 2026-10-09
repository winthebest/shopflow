from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def async_dsn(url: str) -> str:
    """CNPG's `uri` secret and libpq-style URLs say postgresql://; SQLAlchemy needs the asyncpg driver name."""
    scheme, sep, rest = url.partition("://")
    if sep and scheme in ("postgres", "postgresql"):
        return f"postgresql+asyncpg://{rest}"
    return url


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(async_dsn(url), pool_pre_ping=True, pool_size=10, max_overflow=10)
