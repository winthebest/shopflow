{#
  `relationships` for silver tables, which read bronze at different moments: each silver model is its own Cosmos
  task, and the Iceberg sink commits every table on its own. A child can then be newer than everything its parent
  table holds, e.g. an order item whose order reached bronze just after silver `orders` was built, and a plain
  `relationships` test fails on it until the next run (a retry re-runs only the test, on the same tables). So only
  children older than the parent table's newest change must find their parent. `_source_ts_ms` is the source
  commit time, the same for a parent and a child written in one transaction; strictly older, so a tie never fails.
  A real orphan is still reported as soon as the parent table has moved past it. Right after a re-snapshot, while
  the parent table has no change newer than its snapshot, children read later in the snapshot are skipped.
#}
{% test relationships_up_to_parent_cut(model, column_name, to, field) %}
with parent_cut as (
    select max(_source_ts_ms) as source_ts_ms
    from {{ to }}
)

select child.{{ column_name }}
from {{ model }} as child
cross join parent_cut
left join {{ to }} as parent on child.{{ column_name }} = parent.{{ field }}
where
    child.{{ column_name }} is not null
    and parent.{{ field }} is null
    and child._source_ts_ms < parent_cut.source_ts_ms
{% endtest %}
