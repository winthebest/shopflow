// Shop load test: browse (GET /products) then checkout (POST /checkout), driven at a fixed arrival rate.
//
//   k6 run loadtest/checkout.js                                  # constant 20 checkouts/s for 5m
//   k6 run -e PROFILE=ramp -e RATE=50 loadtest/checkout.js       # 0 -> 50/s, hold, ramp down
//   k6 run --log-format raw --console-output out/acks.jsonl loadtest/checkout.js  # ack log (RPO, Phase 7)
//
// Every acknowledged order (HTTP 201) is logged as one JSON line {"ack":...} with its id, status, the client time it
// was acked, its Idempotency-Key and the number of attempts (a retry must return the order created for that key).
// The restore drill (scripts/restore-drill.sh) reads them: acked ids missing after a restore are lost orders, and
// RPO = acked_at of the last ack before the disaster - acked_at of the last one that survived, both on k6's clock.
// `make app-loadtest` writes these lines to out/ (git-ignored).
//
// Every checkout carries a fresh Idempotency-Key (a UUID). With RETRIES > 0, a checkout answered 409/502/503/504
// is retried up to RETRIES times with the same key and body, after the server's Retry-After (else 1s): the retry
// gets the order the first attempt created, never a second one. Retries are tagged `checkout_retry`, so the
// checkout SLO thresholds still measure first attempts.
//
// Env: BASE_URL (http://localhost:8000), PROFILE (constant|ramp), RATE (checkouts/s, 20), DURATION (5m),
//      RAMP_UP (1m), CUSTOMERS (100: seed ids 1..100), MAX_VUS (200), RETRIES (0).
import { check, sleep } from 'k6';
import http from 'k6/http';
import { Counter } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const PROFILE = __ENV.PROFILE || 'constant';
const RATE = Number(__ENV.RATE || 20);
const DURATION = __ENV.DURATION || '5m';
const RAMP_UP = __ENV.RAMP_UP || '1m';
const CUSTOMERS = Number(__ENV.CUSTOMERS || 100);
const MAX_VUS = Number(__ENV.MAX_VUS || 200);
const RETRIES = Number(__ENV.RETRIES || 0);
const RETRYABLE = new Set([409, 502, 503, 504]);

// Each iteration = 1 browse + 1 checkout, so HTTP requests/s = 2 x RATE.
const SCENARIOS = {
  constant: {
    executor: 'constant-arrival-rate',
    rate: RATE,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: Math.min(MAX_VUS, Math.max(10, RATE)),
    maxVUs: MAX_VUS,
  },
  ramp: {
    executor: 'ramping-arrival-rate',
    startRate: 0,
    timeUnit: '1s',
    preAllocatedVUs: Math.min(MAX_VUS, Math.max(10, RATE)),
    maxVUs: MAX_VUS,
    stages: [
      { target: RATE, duration: RAMP_UP },
      { target: RATE, duration: DURATION },
      { target: 0, duration: '30s' },
    ],
  },
};

if (!SCENARIOS[PROFILE]) {
  throw new Error(`PROFILE must be one of ${Object.keys(SCENARIOS).join(', ')}, got ${PROFILE}`);
}

export const options = {
  scenarios: { checkout: SCENARIOS[PROFILE] },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  // Generous limits: they make k6 report per-endpoint numbers and flag a broken stack, not an SLO.
  thresholds: {
    'http_req_duration{name:products}': ['p(99)<1000'],
    'http_req_duration{name:checkout}': ['p(99)<1000'],
    'http_req_failed{name:checkout}': ['rate<0.01'],
    checks: ['rate>0.99'],
  },
};

const ordersPaid = new Counter('orders_paid');
const ordersFailed = new Counter('orders_failed');
const checkoutRetries = new Counter('checkout_retries');

export function setup() {
  const res = http.get(`${BASE_URL}/products`, { tags: { name: 'setup' } });
  if (res.status !== 200) {
    throw new Error(`GET /products returned ${res.status}; is the stack up (make dev) and seeded?`);
  }
  const productIds = res.json().map((p) => p.id);
  if (productIds.length === 0) {
    throw new Error('no products: run the seed');
  }
  return { productIds };
}

function randomInt(min, max) {
  return min + Math.floor(Math.random() * (max - min + 1));
}

function pickItems(productIds) {
  const count = randomInt(1, Math.min(3, productIds.length));
  const chosen = new Set();
  while (chosen.size < count) {
    chosen.add(productIds[randomInt(0, productIds.length - 1)]);
  }
  return [...chosen].map((id) => ({ product_id: id, quantity: randomInt(1, 3) }));
}

export default function (data) {
  const browse = http.get(`${BASE_URL}/products`, { tags: { name: 'products' } });
  check(browse, { 'products 200': (r) => r.status === 200 });

  const payload = JSON.stringify({ customer_id: randomInt(1, CUSTOMERS), items: pickItems(data.productIds) });
  const headers = { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() };
  let res = http.post(`${BASE_URL}/checkout`, payload, { headers, tags: { name: 'checkout' } });
  let attempts = 1;
  for (let retry = 1; retry <= RETRIES && RETRYABLE.has(res.status); retry++) {
    sleep(Number(res.headers['Retry-After']) || 1);
    res = http.post(`${BASE_URL}/checkout`, payload, { headers, tags: { name: 'checkout_retry' } });
    checkoutRetries.add(1);
    attempts++;
  }
  const acked = check(res, { 'checkout 201': (r) => r.status === 201 });
  if (!acked) {
    return;
  }
  const order = res.json();
  (order.status === 'paid' ? ordersPaid : ordersFailed).add(1);
  console.log(
    JSON.stringify({
      ack: {
        order_id: order.id,
        status: order.status,
        acked_at: new Date().toISOString(),
        idempotency_key: headers['Idempotency-Key'],
        attempts: attempts,
      },
    }),
  );
}
