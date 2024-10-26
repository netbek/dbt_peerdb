{% macro synced_after_predicate(datetime) -%}
    {{ adapter.dispatch('synced_after_predicate', 'dbt_peerdb')(datetime) }}
{%- endmacro %}


{% macro clickhouse__synced_after_predicate(datetime) -%}
    _peerdb_synced_at > toDateTime64('{{ datetime }}', 9)
{%- endmacro %}
