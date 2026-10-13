"""CPU per checkout benchmark (docs/perf-baseline.md, 2026-10-13). Not a correctness test: excluded by default.

gateway -> orders -> payments in one process (`shop_client`), orders on a real Postgres. Requests run one at a time:
no pool waits, no queueing, no error path, only the code's own cost per request. Each run measures 2,000 `GET
/products` (the control: unchanged code) and 2,000 checkouts, and prints one JSON line: CPU and wall time per request,
SQL statements per checkout (SQLAlchemy cursor events) and transactions per checkout (`pg_stat_database`).

Two revisions are compared with scripts/app-bench-checkout-cpu.sh, which copies this file into each `git archive` of
them and interleaves the runs. One run by hand, at the current checkout:
    uv run pytest -m bench services/orders/tests/integration/test_bench_checkout.py -s
Env: BENCH_N (2000), BENCH_WARMUP (200), BENCH_KEY (1 = a fresh Idempotency-Key per checkout), BENCH_LABEL,
BENCH_OUT (JSON lines appended there as well).
"""

import asyncio
import json
import os
import time
import uuid

import asyncpg
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

# `integration` too: `make` targets that select `-m "not integration"` replace the default `-m "not bench"`.
pytestmark = [pytest.mark.integration, pytest.mark.bench]

_statements = 0


def _count(*_args, **_kwargs) -> None:
    global _statements
    _statements += 1


async def _transactions(url: str) -> int:
    """Committed + rolled back transactions of this database, read on a fresh connection (no stats snapshot reuse)."""
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetchval(
            "SELECT xact_commit + xact_rollback FROM pg_stat_database WHERE datname = current_database()"
        )
    finally:
        await conn.close()


async def test_bench_checkout(seeded_db, shop_client):
    n = int(os.environ.get("BENCH_N", "2000"))
    warmup = int(os.environ.get("BENCH_WARMUP", "200"))
    with_key = os.environ.get("BENCH_KEY", "0") == "1"
    event.listen(Engine, "before_cursor_execute", _count)
    try:
        async with shop_client(seeded_db) as client:

            async def checkout(i: int) -> None:
                headers = {"Idempotency-Key": str(uuid.uuid4())} if with_key else {}
                body = {"customer_id": i % 100 + 1, "items": [{"product_id": i % 20 + 1, "quantity": 1}]}
                r = await client.post("/checkout", json=body, headers=headers)
                assert r.status_code == 201, r.text

            async def products() -> None:
                r = await client.get("/products")
                assert r.status_code == 200, r.text

            for i in range(warmup):
                await checkout(i)
                await products()

            cpu, wall = time.process_time(), time.perf_counter()
            for _ in range(n):
                await products()
            products_cpu, products_wall = time.process_time() - cpu, time.perf_counter() - wall

            await asyncio.sleep(1.5)  # backends flush their transaction counters when idle
            tx0, st0 = await _transactions(seeded_db), _statements
            cpu, wall = time.process_time(), time.perf_counter()
            for i in range(n):
                await checkout(warmup + i)
            checkout_cpu, checkout_wall = time.process_time() - cpu, time.perf_counter() - wall
            statements = _statements - st0
            await asyncio.sleep(1.5)
            tx1 = await _transactions(seeded_db)
    finally:
        event.remove(Engine, "before_cursor_execute", _count)

    result = {
        "label": os.environ.get("BENCH_LABEL", "run"),
        "key": with_key,
        "n": n,
        "checkout_cpu_ms": round(checkout_cpu / n * 1000, 3),
        "checkout_wall_ms": round(checkout_wall / n * 1000, 3),
        "products_cpu_ms": round(products_cpu / n * 1000, 3),
        "products_wall_ms": round(products_wall / n * 1000, 3),
        "statements_per_checkout": round(statements / n, 3),
        "transactions_per_checkout": round((tx1 - tx0 - 1) / n, 3),  # minus the second counter read
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(json.dumps(result))
    if out := os.environ.get("BENCH_OUT"):
        _append(out, json.dumps(result))


def _append(path: str, line: str) -> None:
    with open(path, "a") as f:
        f.write(line + "\n")
