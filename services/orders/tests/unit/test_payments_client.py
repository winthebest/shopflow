"""PaymentsClient: classification of answers, retries inside the 800ms deadline, circuit breaker, metrics."""

import asyncio
import time
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import orders.payments_client as payments_client
from orders.db import async_dsn
from orders.main import create_app
from orders.payments_client import ChargeOutcome, PaymentsClient
from orders.resilience import BreakerConfig, CircuitState, RetryPolicy
from orders.settings import Settings

UNREACHABLE_DB = "postgresql://shop_app:x@127.0.0.1:9/shop"  # create_app needs one; these tests never query it


async def no_sleep(seconds: float) -> None:
    pass


@asynccontextmanager
async def payments_with(handler, policy: RetryPolicy | None = None, breaker: BreakerConfig | None = None, **kwargs):
    reader = InMemoryMetricReader()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://payments") as http:
        client = PaymentsClient(
            http, policy, breaker, sleep=no_sleep, meter=MeterProvider(metric_readers=[reader]).get_meter("t"), **kwargs
        )
        client.reader = reader
        yield client


async def charge_once(client: PaymentsClient) -> ChargeOutcome:
    permit = client.admit()
    assert permit is not None
    return await client.charge(1, Decimal("9.90"), permit)


async def charge_with(handler, **kwargs) -> ChargeOutcome:
    async with payments_with(handler, **kwargs) as client:
        return await charge_once(client)


def counting(handler):
    """Wrap a handler so the test can see how many attempts reached payments."""

    def wrapped(request):
        wrapped.calls += 1
        return handler(request)

    wrapped.calls = 0
    return wrapped


def points(client: PaymentsClient, name: str) -> dict[tuple, int]:
    data = client.reader.get_metrics_data()
    return {
        tuple(sorted(point.attributes.items())): point.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == name
        for point in metric.data.data_points
    }


async def test_201_is_succeeded_with_charge_id():
    outcome = await charge_with(lambda r: httpx.Response(201, json={"charge_id": "c-1", "status": "succeeded"}))
    assert outcome == ChargeOutcome("succeeded", charge_id="c-1")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(402, json={"charge_id": None, "status": "declined"}), ChargeOutcome("declined")),
        (httpx.Response(201, text="not json"), ChargeOutcome("error", error="unavailable")),
        (httpx.Response(201, json={}), ChargeOutcome("error", error="unavailable")),
        (httpx.Response(500), ChargeOutcome("error", error="unavailable")),
        (httpx.Response(400), ChargeOutcome("error", error="unavailable")),
    ],
    ids=["402-declined", "201-not-json", "201-no-charge-id", "500", "400"],
)
async def test_answers_that_are_not_retried(response, expected):
    handler = counting(lambda r: response)
    assert await charge_with(handler) == expected
    assert handler.calls == 1  # a decline is a business outcome; a bug does not go away on retry


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_gateway_errors_are_retried_up_to_three_attempts(status):
    handler = counting(lambda r: httpx.Response(status))
    assert await charge_with(handler) == ChargeOutcome("error", error="unavailable")
    assert handler.calls == 3


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.RemoteProtocolError("peer closed connection")], ids=type
)
async def test_transport_errors_are_retried_up_to_three_attempts(error):
    def raise_(request):
        raise error

    handler = counting(raise_)
    assert await charge_with(handler) == ChargeOutcome("error", error="unavailable")
    assert handler.calls == 3


async def test_retry_after_a_transient_error_succeeds_with_the_same_request():
    seen = []

    def handler(request):
        seen.append(request.read())
        return httpx.Response(503) if len(seen) == 1 else httpx.Response(201, json={"charge_id": "c-2"})

    assert await charge_with(handler) == ChargeOutcome("succeeded", charge_id="c-2")
    assert seen == [b'{"order_id":1,"amount":"9.90"}'] * 2  # same order id: payments answers idempotently


async def test_slow_attempt_is_cut_and_retried_within_the_deadline():
    calls = []

    async def handler(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            await asyncio.sleep(1)  # this pod hangs
        return httpx.Response(201, json={"charge_id": "c-3"})

    start = time.monotonic()
    outcome = await charge_with(handler, policy=RetryPolicy(attempt_timeout_s=0.1))
    assert outcome == ChargeOutcome("succeeded", charge_id="c-3")
    assert 0.09 < calls[1] - start < 0.5  # cut at the attempt timeout, not after the 1s hang


async def test_deadline_bounds_all_attempts_together(monkeypatch):
    monkeypatch.setattr(payments_client, "PAYMENTS_TIMEOUT_S", 0.25)

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(201, json={"charge_id": "late"})

    handler = counting(handler)
    start = time.monotonic()
    policy = RetryPolicy(attempt_timeout_s=0.2, min_attempt_s=0.02, backoff_base_s=0.001)  # backoff out of the way
    outcome = await charge_with(handler, policy=policy)
    assert outcome == ChargeOutcome("error", error="timeout")
    # 0.2, then the second attempt is trimmed to the ~0.05 left (untrimmed it would run 0.2 more: 0.4 in total)
    assert time.monotonic() - start < 0.32
    assert handler.calls == 2


async def test_uniformly_slow_payments_below_the_attempt_timeout_still_succeeds():
    """A degradation (every answer slow, but inside the attempt timeout) must not become an outage."""

    async def handler(request):
        await asyncio.sleep(0.15)
        return httpx.Response(201, json={"charge_id": "slow"})

    outcome = await charge_with(handler, policy=RetryPolicy(attempt_timeout_s=0.3))
    assert outcome == ChargeOutcome("succeeded", charge_id="slow")


async def test_open_circuit_refuses_and_counts_rejections():
    async with payments_with(lambda r: httpx.Response(503), breaker=BreakerConfig(min_calls=3)) as client:
        await charge_once(client)  # 3 failed attempts open it
        assert client.breaker.state is CircuitState.OPEN
        assert client.admit() is None
        assert points(client, "orders.payments.circuit.rejected") == {(): 1}
        assert points(client, "orders.payments.circuit.transitions") == {(("to", "open"),): 1}
        assert points(client, "orders.payments.circuit.state") == {(): int(CircuitState.OPEN)}


async def test_circuit_opening_mid_charge_stops_the_retries():
    handler = counting(lambda r: httpx.Response(503))
    async with payments_with(handler, breaker=BreakerConfig(min_calls=2)) as client:
        assert await charge_once(client) == ChargeOutcome("error", error="unavailable")
    assert handler.calls == 2  # the third attempt is refused by the now-open circuit


async def test_attempts_are_counted_by_outcome_and_attempt_number():
    replies = iter([httpx.Response(503), httpx.Response(201, json={"charge_id": "c-4"})])
    async with payments_with(lambda r: next(replies)) as client:
        await charge_once(client)
        assert points(client, "orders.payments.attempts") == {
            (("attempt", 1), ("outcome", "unavailable")): 1,
            (("attempt", 2), ("outcome", "succeeded")): 1,
        }


async def test_cancelled_probe_frees_the_half_open_slot():
    clock = [0.0]

    async def hang(request):
        await asyncio.sleep(10)

    config = BreakerConfig(min_calls=1, open_s=5)
    async with payments_with(hang, breaker=config, clock=lambda: clock[0]) as client:
        client.breaker.record(client.admit(), failed=True)
        clock[0] = 5.0
        probe = client.admit()
        assert probe.probe
        task = asyncio.create_task(client.charge(1, Decimal("1.00"), probe))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.admit() is not None  # another probe may start


async def serve_payments(hang_first_charge: bool, peers: list[tuple[str, int]]):
    """Minimal HTTP/1.1 keep-alive server on localhost recording (path, client port) per request; the first charge
    hangs like a request routed to a slow pod."""
    state = {"charges": 0}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        port = writer.get_extra_info("peername")[1]
        try:
            while head := await reader.readuntil(b"\r\n\r\n"):
                lines = head.decode().split("\r\n")
                path = lines[0].split()[1]
                length = next((int(h.split(":")[1]) for h in lines if h.lower().startswith("content-length")), 0)
                await reader.readexactly(length)
                peers.append((path, port))
                if path == "/charges":
                    state["charges"] += 1
                    if hang_first_charge and state["charges"] == 1:
                        await asyncio.sleep(1)  # well past the 0.2s attempt timeout
                else:
                    await asyncio.sleep(0.05)  # keep warm-up requests in flight together: one connection each
                body = b'{"charge_id": "c-new", "status": "succeeded"}'
                writer.write(b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n")
                writer.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


@pytest.mark.parametrize("wired_retries", [True, False], ids=["retry-client-of-create_app", "pooled-retries"])
async def test_retry_after_a_timeout_opens_a_new_connection(wired_retries):
    """kube-proxy picks a pod per TCP connection: the retry must not reuse an idle connection, which may lead to the
    pod that just timed out. Uses create_app's own clients, both warmed first, so keep-alive on the retry client would
    be caught too. Control case: retrying over the pool does reuse a warm connection."""
    peers: list[tuple[str, int]] = []
    server = await serve_payments(hang_first_charge=True, peers=peers)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    app = create_app(
        Settings(database_url=UNREACHABLE_DB, payments_url=url, payments_attempt_timeout_ms=200, sweep_interval_s=0)
    )
    async with server, app.router.lifespan_context(app):
        wired: PaymentsClient = app.state.payments
        await asyncio.gather(wired.http.get("/warm"), wired.http.get("/warm"))  # two idle pooled connections
        await wired.retry_http.get("/warm")  # would stay idle and be reused if the retry client kept connections
        warm = {port for path, port in peers if path == "/warm"}
        assert len(warm) == 3
        client = wired if wired_retries else PaymentsClient(wired.http, wired.policy, sleep=no_sleep)
        outcome = await client.charge(1, Decimal("9.90"), client.admit())

    assert outcome == ChargeOutcome("succeeded", charge_id="c-new")
    first, retry = [port for path, port in peers if path == "/charges"]
    assert first in warm  # the first attempt rides the pool
    assert (retry not in warm) is wired_retries


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@h:5432/shop", "postgresql+asyncpg://u:p@h:5432/shop"),
        ("postgres://u:p@h/shop", "postgresql+asyncpg://u:p@h/shop"),
        ("postgresql+asyncpg://u:p@h/shop", "postgresql+asyncpg://u:p@h/shop"),
        ("postgresql://u:p@h/shop?sslmode=require", "postgresql+asyncpg://u:p@h/shop?ssl=require"),
        ("postgresql://u:p%40ss@h/shop", "postgresql+asyncpg://u:p%40ss@h/shop"),
    ],
)
def test_async_dsn(url, expected):
    assert async_dsn(url) == expected
