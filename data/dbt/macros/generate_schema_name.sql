{# Custom schemas are used as they are (silver, gold), not dbt's default "<profile schema>_<custom schema>". #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ (custom_schema_name or target.schema) | trim }}
{%- endmacro %}
