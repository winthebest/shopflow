from shopflow_common.db import create_engine

URL = "postgresql://u:p@127.0.0.1:9/shop"  # never connected: creating an engine opens no connection


async def test_pool_defaults_and_explicit_size():
    default, small = create_engine(URL), create_engine(URL, pool_size=2, max_overflow=0)
    try:
        assert (default.pool.size(), default.pool._max_overflow) == (10, 10)
        assert (small.pool.size(), small.pool._max_overflow) == (2, 0)
    finally:
        await default.dispose()
        await small.dispose()
