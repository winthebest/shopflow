-- One row per customer. No email or name: gold is what Metabase reads, and dashboards need no PII.
with orders as (
    select
        customer_id,
        count(*) as order_count,
        min(created_at) as first_order_at,
        max(created_at) as last_order_at
    from {{ ref('orders') }}
    group by customer_id
)

select
    c.id as customer_id,
    c.created_at,
    o.first_order_at,
    o.last_order_at,
    coalesce(o.order_count, 0) as order_count
from {{ ref('customers') }} as c
left join orders as o on c.id = o.customer_id
