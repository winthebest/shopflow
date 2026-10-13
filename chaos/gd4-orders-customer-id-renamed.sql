-- Game day 4 — a migration that bypasses CI renames orders.customer_id (Phase 7 scenario 4). docs/runbooks/gameday.md.
-- Simulates a release whose migration and ORM renamed the column while data/contracts and silver were not updated (the
--   contract gate in CI would have refused it). The running app still writes customer_id, so a nullable shim column takes
--   the value and a trigger moves it into buyer_id: checkout keeps working, and CDC emits customer_id = null. The sink
--   (schema evolution off) drops buyer_id, so bronze sees what a real rename would give it.
-- Hypothesis: every order inserted or updated from T0 reaches bronze.orders with customer_id = null; silver orders'
--   not_null test fails at the next hourly dbt_build (one retry, 5 minutes later), and AirflowDagRunFailed (ticket,
--   for 5m) fires: detection = time to the next scheduled run + ~10 minutes. Checkout SLOs stay green.
-- Side effects: GET /orders/{id} and the replay of a settled order fail: orders answers 500 (OrderOut needs
--   customer_id, now null on every row) and the gateway passes it on as 502. So k6 runs with RETRIES=0.
-- Blast radius: the data pipeline from bronze.orders on (silver, gold, reconciliation); checkout unaffected.
-- Before: dump table orders into the run's output (docs/runbooks/gameday.md). Restore:
--   chaos/gd4-orders-customer-id-restored.sql with the t0 printed below.
-- One transaction: a rename committed without the shim column would fail every checkout insert. lock_timeout keeps the
--   ACCESS EXCLUSIVE lock from queueing behind checkouts and blocking them; on a lock timeout, run it again.
-- Run as the table owner: psql -v ON_ERROR_STOP=1 -f chaos/gd4-orders-customer-id-renamed.sql
BEGIN;
SET LOCAL lock_timeout = '2s';
SELECT now() AS t0;
ALTER TABLE orders RENAME COLUMN customer_id TO buyer_id;
ALTER TABLE orders ADD COLUMN customer_id bigint;
CREATE FUNCTION gd4_orders_buyer_shim() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.buyer_id := NEW.customer_id;
  NEW.customer_id := NULL;
  RETURN NEW;
END
$$;
CREATE TRIGGER gd4_orders_buyer_shim BEFORE INSERT ON orders FOR EACH ROW EXECUTE FUNCTION gd4_orders_buyer_shim();
COMMIT;
