-- The CDC epoch silver is built from: the newest one whose Debezium snapshot has completed.
select max(epoch) as epoch
from {{ source('pg_meta', 'cdc_epochs') }}
where snapshot_completed_at is not null
