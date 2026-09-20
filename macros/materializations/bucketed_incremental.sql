{# Source: https://github.com/ClickHouse/dbt-clickhouse/blob/v1.10.2/dbt/include/clickhouse/macros/materializations/incremental/incremental.sql #}
{% materialization bucketed_incremental, adapter='clickhouse' %}

  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='table') -%}

  {%- set unique_key = config.get('unique_key') -%}
  {% if unique_key is not none and unique_key|length == 0 %}
    {% set unique_key = none %}
  {% endif %}
  {% if unique_key is iterable and (unique_key is not string and unique_key is not mapping) %}
     {% set unique_key = unique_key|join(', ') %}
  {% endif %}
  {%- set inserts_only = config.get('inserts_only') -%}
  {%- set grant_config = config.get('grants') -%}
  {%- set has_contract = config.get('contract').enforced -%}
  {%- set full_refresh_mode = (should_full_refresh() or existing_relation is none or existing_relation.is_view) -%}
  {%- set on_schema_change = incremental_validate_on_schema_change(config.get('on_schema_change'), default='ignore') -%}
  {%- set bucket_column = config.get('bucket_column', none) -%}
  {%- set bucket_type = config.get('bucket_type', none) -%}
  {%- set rows_per_bucket = config.get('rows_per_bucket', 1000000) -%}
  {%- set bucket_source_table = config.get('bucket_source_table', none) -%}
  {%- set marker = '-- __BUCKET_PREDICATE__' -%}

  {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
  {%- set backup_relation_type = 'table' if existing_relation is none else existing_relation.type -%}
  {%- set backup_relation = make_backup_relation(target_relation, backup_relation_type) -%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation) -%}
  {%- set preexisting_backup_relation = load_cached_relation(backup_relation) -%}

  {% if bucket_column is none %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_column is required (the immutable key column).'
    ) }}
  {% endif %}
  {% if bucket_source_table is none %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_source_table is required (e.g. "my_database.my_table").'
    ) }}
  {% endif %}
  {% if bucket_type not in ('uuid', 'uint', 'int') %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_type must be one of "uuid", "uint", "int".'
    ) }}
  {% endif %}
  {% if unique_key != bucket_column %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: unique_key must be the single bucket_column, '
        ~ 'otherwise versions of one row split across buckets.'
    ) }}
  {% endif %}
  {% if rows_per_bucket is not number or rows_per_bucket | int != rows_per_bucket or rows_per_bucket < 1 %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: rows_per_bucket must be a positive integer.'
    ) }}
  {% endif %}
  {% if bucket_type == 'uuid' %}
    {% set bucket_lhs = 'reinterpretAsUInt64(' ~ bucket_column ~ ')' %}
  {% else %}
    {% set bucket_lhs = bucket_column %}
  {% endif %}

  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}
  {{ drop_relation_if_exists(preexisting_backup_relation) }}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}
  {% set to_drop = [] %}
  {% set need_swap = false %}

  {% if full_refresh_mode %}
    {% set marker_count = sql.count(marker) %}
    {% if marker_count != 1 %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: marker ' ~ marker ~ ' must appear exactly once in the model SQL; found '
          ~ marker_count ~ '.'
      ) }}
    {% endif %}
    {% set bucket_parts = bucket_source_table.replace('"', '').replace("'", '').replace('`', '').split('.') %}
    {% if bucket_parts | length != 2 %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_source_table must have the form "database.table", got "'
          ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% set bucket_relation = adapter.get_relation(
        database=bucket_parts[0], schema=bucket_parts[0], identifier=bucket_parts[1]
    ) %}
    {% if bucket_relation is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
          ~ '" not found for model "' ~ model.name ~ '".'
      ) }}
    {% endif %}
    {#- Every rebuild fills the intermediate relation and is published at the
        end, so readers never see a half-built table and a failed build leaves
        the target untouched. Publishing is a plain rename on the first run,
        an atomic exchange when the existing table supports it, or two renames
        otherwise (views and engines without atomic exchange report
        can_exchange=False). -#}
    {% set build_relation = intermediate_relation %}
    {% set need_swap = true %}
    {% if bucket_type in ('int', 'uint') %}
      {% set count_sql %}select count() as row_count, countIf({{ bucket_column }} < 0) as negative_key_count from {{ bucket_source_table }}{% endset %}
    {% else %}
      {% set count_sql %}select count() as row_count from {{ bucket_source_table }}{% endset %}
    {% endif %}
    {% set count_result = run_query(count_sql) %}
    {% set row_count = count_result.columns[0].values()[0] | int %}
    {% if bucket_type in ('int', 'uint') %}
      {% set negative_key_count = count_result.columns[1].values()[0] | int %}
      {% if negative_key_count > 0 %}
        {{ exceptions.raise_compiler_error(
            'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
            ~ '" has ' ~ negative_key_count ~ ' negative values in "' ~ bucket_column
            ~ '". Negative integer keys cannot be bucketed (the bucket maths uses '
            ~ 'modulo), so a full refresh would silently drop them. Use a '
            ~ 'non-negative key column.'
        ) }}
      {% endif %}
    {% endif %}
    {% set bucket_count = ((row_count / rows_per_bucket) | round(0, 'ceil')) | int %}
    {{ log(
        'bucketed_incremental: row_count='
        ~ row_count
        ~ ' bucket_count='
        ~ bucket_count,
        info=True,
    ) }}
    {% if bucket_count < 1 %}
      {% set empty_sql = sql.replace(marker, 'where 1 = 0') %}
      {% call statement('main') %}
        {{ get_create_table_as_sql(False, build_relation, empty_sql) }}
      {% endcall %}
    {% else %}
      {% for i in range(bucket_count) %}
        {% set predicate = 'where '
            ~ bucket_lhs
            ~ ' % '
            ~ bucket_count
            ~ ' = '
            ~ i %}
        {% set bucket_sql = sql.replace(marker, predicate) %}
        {{ log(
            'bucketed_incremental: Processing bucket ' ~ (i + 1) ~ ' of ' ~ bucket_count, info=True
        ) }}
        {% if loop.first %}
          {% call statement('main') %}
            {{ get_create_table_as_sql(False, build_relation, bucket_sql) }}
          {% endcall %}
        {% else %}
          {% call statement('bucket_' ~ i) %}
            {{ clickhouse__insert_into(build_relation, bucket_sql, has_contract) }}
          {% endcall %}
        {% endif %}
      {% endfor %}
    {% endif %}

  {% else %}
    {% if inserts_only %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: inserts_only is not supported; incremental runs always use delete+insert.'
      ) }}
    {% endif %}
    {% set incremental_strategy = adapter.calculate_incremental_strategy(config.get('incremental_strategy')) %}
    {% set incremental_predicates = config.get('predicates', []) or config.get('incremental_predicates', []) %}
    {% if incremental_strategy != 'delete_insert' %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: only the delete_insert incremental strategy is supported, got '
          ~ incremental_strategy ~ '.'
      ) }}
    {% endif %}
    {% set partition_by = config.get('partition_by') %}
    {% do adapter.validate_incremental_strategy(incremental_strategy, incremental_predicates, unique_key, partition_by) %}
    {%- if on_schema_change != 'ignore' %}
      {%- set column_changes = adapter.check_incremental_schema_changes(on_schema_change, existing_relation, sql, query_settings=config.get('query_settings', {})) -%}
      {% if column_changes %}
        {% do clickhouse__apply_column_changes(column_changes, existing_relation) %}
        {% set existing_relation = load_cached_relation(this) %}
      {% endif %}
    {% endif %}
    {% do clickhouse__incremental_delete_insert(
        existing_relation, unique_key, incremental_predicates
    ) %}
  {% endif %}

  {% if need_swap %}
      {% if existing_relation is none %}
        {% do adapter.rename_relation(intermediate_relation, target_relation) %}
      {% elif existing_relation.can_exchange %}
        {% do adapter.rename_relation(intermediate_relation, backup_relation) %}
        {% do exchange_tables_atomic(backup_relation, target_relation) %}
        {% do to_drop.append(backup_relation) %}
      {% else %}
        {% do adapter.rename_relation(target_relation, backup_relation) %}
        {% do adapter.rename_relation(intermediate_relation, target_relation) %}
        {% do to_drop.append(backup_relation) %}
      {% endif %}
  {% endif %}

  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}

  {% do persist_docs(target_relation, model) %}

  {% if existing_relation is none or existing_relation.is_view or should_full_refresh() %}
    {% do create_indexes(target_relation) %}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {% do adapter.commit() %}

  {% for rel in to_drop %}
      {% do adapter.drop_relation(rel) %}
  {% endfor %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target_relation]}) }}

{%- endmaterialization %}
