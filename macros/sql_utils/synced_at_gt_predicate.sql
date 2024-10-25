{% macro synced_at_gt_predicate(datetime) -%}
    {{ adapter.dispatch('synced_at_gt_predicate', 'dbt_audit')(datetime) }}
{%- endmacro %}


{% macro clickhouse__synced_at_gt_predicate(datetime) -%}
    _peerdb_synced_at > toDateTime64('{{ datetime }}', 9)
{%- endmacro %}
