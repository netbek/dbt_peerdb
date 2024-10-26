{% macro last_synced_at(identifier) -%}
    {{ adapter.dispatch('last_synced_at', 'dbt_peerdb')(identifier) }}
{%- endmacro %}


{% macro clickhouse__last_synced_at(identifier) -%}
    {% if not execute %}
        {{ return(none) }}
    {% else %}
        {% set relation = adapter.get_relation(this.database, this.schema, identifier) %}
        {% if relation %}
            {% set sql %}
                select maxOrNull(_peerdb_synced_at) as last_synced_at
                from {{ relation }}
            {% endset %}
            {% set value = dbt_utils.get_single_value(sql) %}
            {{ return(value) }}
        {% else %}
            {{ return(none) }}
        {% endif %}
    {% endif %}
{%- endmacro %}
