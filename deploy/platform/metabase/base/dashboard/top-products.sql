-- The 10 products with the most revenue from paid orders over the last 30 days (gold.mart_product_sales).
SELECT
    product_name,
    sum(units) AS units,
    sum(revenue) AS revenue
FROM gold.mart_product_sales
WHERE order_date >= current_date - INTERVAL '30' DAY
GROUP BY product_name
ORDER BY revenue DESC
LIMIT 10
