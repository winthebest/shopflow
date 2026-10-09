{#
  Bronze rows of the latest CDC epoch whose Debezium snapshot has completed (pg.meta.cdc_epochs, written by
  scripts/cdc-epoch.sh; docs/adr/0406). Older epochs stay in bronze as history and are never mixed in: a key that is
  missing from the current epoch was deleted while CDC was down.
#}
{% macro bronze_current_epoch(table) %}
select b.*
from {{ source('bronze', table) }} as b
inner join {{ ref('stg_cdc__current_epoch') }} as e on b._cdc_epoch = e.epoch
{% endmacro %}

{#
  Current state per key: streaming changes win over snapshot reads (_op = 'r'), then the highest LSN (comparable
  within one epoch only, hence one epoch at a time). A key whose last change is a delete (_op = 'd') is dropped.
#}
{% macro latest_state(relation, columns, key='id') %}
with ranked as (
    select
        {{ columns | join(',\n        ') }},
        _op,
        _cdc_epoch,
        _source_ts_ms,
        row_number() over (partition by {{ key }} order by (_op != 'r') desc, _lsn desc) as change_rank
    from {{ relation }}
)

select
    {{ columns | join(',\n    ') }},
    _cdc_epoch,
    _source_ts_ms
from ranked
where change_rank = 1 and _op != 'd'
{% endmacro %}
