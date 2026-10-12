-- mart_product_sales has one row per UTC day and product: a duplicate would double a product in "top products".
select
    order_date,
    product_id,
    count(*) as row_count
from {{ ref('mart_product_sales') }}
group by order_date, product_id
having count(*) > 1
