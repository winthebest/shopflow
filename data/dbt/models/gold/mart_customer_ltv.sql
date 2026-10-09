-- Lifetime value per customer: paid orders only.
select
    customer_id,
    count(*) as paid_orders,
    sum(total) as lifetime_revenue,
    avg(total) as avg_order_value,
    min(paid_at) as first_paid_at,
    max(paid_at) as last_paid_at
from {{ ref('fct_orders') }}
where status = 'paid'
group by customer_id
