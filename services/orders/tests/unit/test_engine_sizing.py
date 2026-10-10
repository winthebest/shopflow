"""orders sizes its pool from settings: what one replica may hold on shop-db's shared max_connections."""

from orders.main import create_app
from orders.settings import Settings

UNREACHABLE_DB = "postgresql://shop_app:x@127.0.0.1:9/shop"


async def pool_of(**settings) -> tuple[int, int]:
    app = create_app(Settings(database_url=UNREACHABLE_DB, **settings))
    async with app.router.lifespan_context(app):
        pool = app.state.engine.pool
        return pool.size(), pool._max_overflow


async def test_default_pool_is_ten_plus_five():
    assert await pool_of() == (10, 5)


async def test_pool_follows_the_settings():
    assert await pool_of(db_pool_size=4, db_max_overflow=2) == (4, 2)
