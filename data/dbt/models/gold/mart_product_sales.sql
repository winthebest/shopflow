-- Units and revenue per UTC day of order creation and product; paid orders only, as revenue in mart_daily_revenue.
select
    o.order_date,
    i.product_id,
    p.sku,
    p.name as product_name,
    count(distinct o.order_id) as orders,
    sum(i.quantity) as units,
    sum(i.quantity * i.unit_price) as revenue
from {{ ref('fct_orders') }} as o
inner join {{ ref('order_items') }} as i on o.order_id = i.order_id
inner join {{ ref('products') }} as p on i.product_id = p.id
where o.status = 'paid'
group by o.order_date, i.product_id, p.sku, p.name
