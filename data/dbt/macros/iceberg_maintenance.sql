{#
  Iceberg maintenance through Trino for every table of one schema in this target's catalog (`lake`): compact data
  files, rewrite manifests, expire snapshots and remove orphan files older than `retention`. Run by the Airflow DAG
  iceberg_maintenance: dbt run-operation iceberg_maintenance --args '{schema: bronze}'.
  `retention` must not be below the catalog floor (7d, iceberg.*.min-retention; Trino rejects anything lower), so a
  file written by a commit in flight is never removed.
#}
{% macro iceberg_maintenance(schema, retention='7d') %}
    {% set tables = run_query(
        "select table_name from " ~ target.database ~ ".information_schema.tables"
        ~ " where table_schema = '" ~ schema ~ "' and table_type = 'BASE TABLE' order by table_name"
    ) %}
    {% for row in tables %}
        {% set relation = target.database ~ '.' ~ schema ~ '.' ~ row[0] %}
        {% for procedure in [
            "optimize",
            "optimize_manifests",
            "expire_snapshots(retention_threshold => '" ~ retention ~ "')",
            "remove_orphan_files(retention_threshold => '" ~ retention ~ "')",
        ] %}
            {% set statement = "alter table " ~ relation ~ " execute " ~ procedure %}
            {% do log(statement, info=True) %}
            {% do run_query(statement) %}
        {% endfor %}
    {% endfor %}
    {% do log(schema ~ ": maintained " ~ (tables | length) ~ " table(s)", info=True) %}
{% endmacro %}
