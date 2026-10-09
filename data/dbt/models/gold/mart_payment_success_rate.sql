-- Payment attempts per UTC day and outcome; success_rate = succeeded / attempts.
select
    cast(created_at at time zone 'UTC' as date) as payment_date,
    count(*) as attempts,
    count_if(status = 'succeeded') as succeeded,
    count_if(status = 'declined') as declined,
    count_if(status = 'error') as errors,
    cast(count_if(status = 'succeeded') as double) / count(*) as success_rate
from {{ ref('payments') }}
group by 1
