-- One row per order with its items and payment outcome. Dates are UTC.
with items as (
    select
        order_id,
        count(*) as item_lines,
        sum(quantity) as item_quantity,
        sum(quantity * unit_price) as items_amount
    from {{ ref('order_items') }}
    group by order_id
),

payments as (
    select
        order_id,
        count(*) as payment_attempts,
        max(case when status = 'succeeded' then created_at end) as paid_at
    from {{ ref('payments') }}
    group by order_id
)

select
    o.id as order_id,
    o.customer_id,
    o.status,
    o.total,
    o.created_at,
    o.updated_at,
    p.paid_at,
    cast(o.created_at at time zone 'UTC' as date) as order_date,
    coalesce(i.item_lines, 0) as item_lines,
    coalesce(i.item_quantity, 0) as item_quantity,
    coalesce(i.items_amount, 0) as items_amount,
    coalesce(p.payment_attempts, 0) as payment_attempts
from {{ ref('orders') }} as o
left join items as i on o.id = i.order_id
left join payments as p on o.id = p.order_id
