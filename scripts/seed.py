"""Seed demo products and customers into DATABASE_URL (idempotent).

Local:      DATABASE_URL=postgresql://shop_app:shop_app@localhost:25432/shop uv run scripts/seed.py
Container:  the orders image ships the same logic as the `seed` command.
"""

from orders.seed import main

if __name__ == "__main__":
    main()
