"""Stranded-order sweeper decisions, with the database calls and payments replaced by fakes."""

import asyncio
from decimal import Decimal

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import orders.sweeper as sweeper_module
from orders.payments_client import ChargeOutcome
from orders.repository import Settled
from orders.resilience import BreakerConfig, CircuitBreaker
from orders.sweeper import Sweeper

NOW = __import__("datetime").datetime(2026, 10, 10, 12, 0, 0)


class FakePayments:
    def __init__(self, *outcomes: ChargeOutcome, open_circuit: bool = False) -> None:
        self.outcomes = list(outcomes)
        self.charged: list[int] = []
        self.breaker = CircuitBreaker(BreakerConfig(min_calls=1, failure_ratio=1.0))
        if open_circuit:
            self.breaker.record(self.breaker.allow(), failed=True)

    async def charge(self, order_id, amount, permit):
        self.charged.append(order_id)
        self.breaker.record(permit, failed=False)
        return self.outcomes.pop(0)


@pytest.fixture
def db(monkeypatch):
    """Fake repository: claimable orders, recorded settles, and who wins each settle."""
    state = {"claimable": [(7, Decimal("10.00")), (8, Decimal("20.00"))], "settles": [], "lost": set(), "age": 42.5}

    async def claim(sessionmaker, stale_after_s, limit):
        claimed, state["claimable"] = state["claimable"][:limit], []
        return claimed

    async def settle(sessionmaker, order_id, amount, outcome):
        state["settles"].append((order_id, outcome.status))
        status = "paid" if outcome.status == "succeeded" else "failed"
        return Settled(status, NOW, settled_now=order_id not in state["lost"])

    async def oldest(sessionmaker):
        return state["age"]

    monkeypatch.setattr(sweeper_module, "claim_stranded_orders", claim)
    monkeypatch.setattr(sweeper_module, "settle_order", settle)
    monkeypatch.setattr(sweeper_module, "oldest_pending_age_s", oldest)
    return state


def sweeper(payments) -> tuple[Sweeper, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("t")
    return Sweeper(None, payments, meter=meter), reader


def points(reader, name):
    data = reader.get_metrics_data()
    return {
        tuple(sorted(p.attributes.items())): p.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == name
        for p in m.data.data_points
    }


async def test_answers_are_settled_and_counted(db, caplog):
    payments = FakePayments(ChargeOutcome("succeeded", charge_id="c-7"), ChargeOutcome("declined"))
    sweep, reader = sweeper(payments)
    assert await sweep.sweep_once() == 2
    assert db["settles"] == [(7, "succeeded"), (8, "declined")]
    assert points(reader, "orders.settle.recovered") == {(("status", "paid"),): 1, (("status", "failed"),): 1}
    assert sum(r.message == "stranded order settled" for r in caplog.records) == 2


async def test_no_answer_leaves_the_order_pending(db):
    payments = FakePayments(ChargeOutcome("error", error="timeout"), ChargeOutcome("succeeded", charge_id="c-8"))
    sweep, _ = sweeper(payments)
    assert await sweep.sweep_once() == 1
    assert db["settles"] == [(8, "succeeded")]  # order 7 is retried once its lease expires


async def test_open_circuit_ends_the_tick_without_charging(db):
    payments = FakePayments(open_circuit=True)
    sweep, _ = sweeper(payments)
    assert await sweep.sweep_once() == 0
    assert payments.charged == [] and db["settles"] == []


async def test_order_settled_meanwhile_by_its_request_is_not_counted(db):
    db["lost"] = {7}
    payments = FakePayments(ChargeOutcome("succeeded", charge_id="c-7"), ChargeOutcome("succeeded", charge_id="c-8"))
    sweep, reader = sweeper(payments)
    assert await sweep.sweep_once() == 1
    assert points(reader, "orders.settle.recovered") == {(("status", "paid"),): 1}


async def test_gauge_reports_the_oldest_pending_age_of_the_last_sweep(db):
    sweep, reader = sweeper(FakePayments(ChargeOutcome("declined"), ChargeOutcome("declined")))
    assert points(reader, "orders.pending.oldest_age_seconds") == {(): 0.0}
    await sweep.sweep_once()
    assert points(reader, "orders.pending.oldest_age_seconds") == {(): 42.5}


async def test_a_failed_sweep_is_logged_and_the_loop_goes_on(db, monkeypatch, caplog):
    async def broken(*args):
        raise ConnectionRefusedError("database down")

    monkeypatch.setattr(sweeper_module, "claim_stranded_orders", broken)
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    sweep = Sweeper(None, FakePayments(), interval_s=3, sleep=sleep, meter=MeterProvider().get_meter("t"))
    with pytest.raises(asyncio.CancelledError):
        await sweep.run()
    assert sleeps == [3, 3]
    assert sum(r.message == "stranded-order sweep failed" for r in caplog.records) == 2
