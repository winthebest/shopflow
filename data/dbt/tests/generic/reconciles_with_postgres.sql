{#
  Exact reconciliation of a silver table with its Postgres source (read through Trino catalog `pg`): a two-way
  anti-join on the key plus the compared columns, one row per mismatch. Silver is as of its last data commit (not
  `replace` snapshots: optimize and manifest rewrites change no rows) and bronze trails Postgres by the CDC lag, so
  rows changed after (last silver data commit - `reconcile_lag_minutes`) on either side are skipped. At a quiet point (no writes, lag ~ 0) run with reconcile_lag_minutes=0 right after a rebuild: silver
  must then equal Postgres exactly. Deletes carry no updated_at, so a row deleted in Postgres inside that window still
  counts as a mismatch: the shop never deletes, and re-snapshot tests run at a quiet point.
#}
{% test reconciles_with_postgres(model, source_table, compare, key='id') %}
{%- set snapshots = model.database ~ '.' ~ model.schema ~ '."' ~ model.identifier ~ '$snapshots"' %}
{%- set cutoff = "(select max(committed_at) from " ~ snapshots ~ " where operation != 'replace') - interval '"
    ~ (var('reconcile_lag_minutes') | int) ~ "' minute" %}
with postgres as (
    select
        {{ key }},
        {{ compare | join(',\n        ') }},
        updated_at
    from {{ source('pg_public', source_table) }}
),

lake as (
    select
        {{ key }},
        {{ compare | join(',\n        ') }},
        updated_at
    from {{ model }}
)

select
    coalesce(postgres.{{ key }}, lake.{{ key }}) as {{ key }},
    case
        when lake.{{ key }} is null then 'missing in silver'
        when postgres.{{ key }} is null then 'missing in postgres'
        else 'values differ'
    end as mismatch
from postgres
full outer join lake on postgres.{{ key }} = lake.{{ key }}
where
    (
        postgres.{{ key }} is null
        or lake.{{ key }} is null
        {%- for column in compare %}
        or postgres.{{ column }} is distinct from lake.{{ column }}
        {%- endfor %}
    )
    and coalesce(postgres.updated_at, lake.updated_at) < {{ cutoff }}
    and coalesce(lake.updated_at, postgres.updated_at) < {{ cutoff }}
{% endtest %}
