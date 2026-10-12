-- Game day 4 restore: undo chaos/gd4-orders-customer-id-renamed.sql, then make CDC send again every order it sent with
--   customer_id = null. Those are exactly the rows inserted or updated since T0: inserts default updated_at to now(),
--   and trigger trg_orders_updated_at sets it on every update. Touching them (the trigger sets updated_at again) makes
--   Debezium emit each row in full, and the next dbt_build is green. One minute of margin covers checkouts whose
--   transaction began before the rename committed.
-- Run as the table owner: psql -v ON_ERROR_STOP=1 -v t0='<t0 printed by the inject>' -f chaos/gd4-orders-customer-id-restored.sql
BEGIN;
SET LOCAL lock_timeout = '2s';
DROP TRIGGER gd4_orders_buyer_shim ON orders;
DROP FUNCTION gd4_orders_buyer_shim();
ALTER TABLE orders DROP COLUMN customer_id;
ALTER TABLE orders RENAME COLUMN buyer_id TO customer_id;
COMMIT;
UPDATE orders SET updated_at = updated_at WHERE updated_at >= :'t0'::timestamptz - interval '1 minute';
SELECT count(*) AS orders_without_customer FROM orders WHERE customer_id IS NULL;
