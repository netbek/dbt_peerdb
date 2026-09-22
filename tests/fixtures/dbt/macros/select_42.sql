{% macro select_42() %}
    {% set query %}
        select 42 as answer
    {% endset %}

    {% set results = run_query(query) %}

    {# If we are in interactive mode (dbt run-operation), print the result to the console #}
    {% if execute %}
        {% do results.print_table() %}
        {{ return(results) }}
    {% endif %}
{% endmacro %}
