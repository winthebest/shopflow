-- Orders and revenue per UTC day of order creation; revenue counts paid orders only.
select
    order_date,
    count(*) as orders,
    count_if(status = 'paid') as paid_orders,
    count_if(status = 'failed') as failed_orders,
    coalesce(sum(case when status = 'paid' then total end), 0) as revenue,
    avg(case when status = 'paid' then total end) as avg_paid_order_value
from {{ ref('fct_orders') }}
group by order_date
