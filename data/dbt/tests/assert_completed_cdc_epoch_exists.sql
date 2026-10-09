-- Fails when no CDC epoch has a completed snapshot (scripts/cdc-epoch.sh wait): silver would otherwise be empty
-- and every other test would still pass.
select 'no row with snapshot_completed_at in pg.meta.cdc_epochs' as problem
where not exists (
    select 1
    from {{ source('pg_meta', 'cdc_epochs') }}
    where snapshot_completed_at is not null
)
