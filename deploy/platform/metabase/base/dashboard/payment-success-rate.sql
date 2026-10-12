-- Share of payment attempts that succeeded per UTC day over the last 30 days (gold.mart_payment_success_rate).
SELECT
    payment_date,
    success_rate,
    attempts
FROM gold.mart_payment_success_rate
WHERE payment_date >= current_date - INTERVAL '30' DAY
ORDER BY payment_date
