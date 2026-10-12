-- Revenue of paid orders per UTC day over the last 30 days (gold.mart_daily_revenue).
SELECT
    order_date,
    revenue,
    paid_orders,
    orders
FROM gold.mart_daily_revenue
WHERE order_date >= current_date - INTERVAL '30' DAY
ORDER BY order_date
