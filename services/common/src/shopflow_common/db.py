"""Async SQLAlchemy engine for the shop database (orders, fulfillment-worker). Needs the `db` extra."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# A request has a 1s end-to-end budget (gateway -> orders): fail fast instead of queueing on a busy pool or
# waiting on a dead host, so orders answers before the gateway gives up.
POOL_TIMEOUT_S = 0.5
CONNECT_TIMEOUT_S = 0.5


def async_dsn(url: str) -> str:
    """CNPG's `uri` secret and libpq URLs say postgresql://...?sslmode=...; asyncpg wants its driver name and `ssl`."""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme not in ("postgres", "postgresql"):
        return url
    parsed = make_url(f"postgresql+asyncpg://{rest}")
    if "sslmode" in parsed.query:
        sslmode = parsed.query["sslmode"]
        parsed = parsed.difference_update_query(["sslmode"]).update_query_dict({"ssl": sslmode})
    return parsed.render_as_string(hide_password=False)


def create_engine(
    url: str,
    *,
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    command_timeout_s: float | None = None,
    pool_size: int = 10,
    max_overflow: int = 10,
) -> AsyncEngine:
    """Defaults fit orders (1s request budget). Background consumers pass longer connect timeouts and a statement
    timeout, so a half-open connection fails fast instead of stalling a partition.

    `pool_size + max_overflow` is what one process may hold on shop-db, whose `max_connections` (100) every replica of
    every service shares: callers size it from their measured use (docs/perf-baseline.md, connection budget)."""
    connect_args: dict = {"timeout": connect_timeout_s}
    if command_timeout_s is not None:
        connect_args["command_timeout"] = command_timeout_s
    return create_async_engine(
        async_dsn(url),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=POOL_TIMEOUT_S,
        connect_args=connect_args,
    )
