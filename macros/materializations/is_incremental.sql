{# Source: https://github.com/ClickHouse/dbt-clickhouse/blob/v1.10.2/dbt/include/clickhouse/macros/materializations/incremental/is_incremental.sql #}
{% macro is_incremental() %}
    {#-- do not run introspective queries in parsing #}
    {% if not execute %}
        {{ return(False) }}
    {% else %}
        {% set relation = adapter.get_relation(this.database, this.schema, this.table) %}
        {{ return(relation is not none
                  and relation.type == 'table'
                  and (model.config.materialized == 'incremental' or model.config.materialized == 'distributed_incremental' or model.config.materialized == 'bucketed_incremental')
                  and not should_full_refresh()) }}
    {% endif %}
{% endmacro %}
