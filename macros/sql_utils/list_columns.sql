{% macro list_columns(relation_alias=none) -%}
    {% for column in var('dbt_peerdb_columns', []) -%}
        assumeNotNull({% if relation_alias %}{{ relation_alias }}.{% endif %}{{ adapter.quote(column) }}) as {{ column }}{% if not loop.last %},{% endif %}
    {% endfor %}
{%- endmacro %}
