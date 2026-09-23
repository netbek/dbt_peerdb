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
  {%- set bucket_key_column = config.get('bucket_key_column', none) -%}
  {%- set rows_per_bucket = config.get('rows_per_bucket', 1000000) -%}
  {%- set bucket_source_table = config.get('bucket_source_table', none) -%}
  {%- set bucket_snapshot_column = config.get('bucket_snapshot_column', none) -%}
  {%- set on_concurrent_writes = config.get('on_concurrent_writes', 'error') -%}
  {%- set marker = '-- __BUCKET_PREDICATE__' -%}

  {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
  {%- set backup_relation_type = 'table' if existing_relation is none else existing_relation.type -%}
  {%- set backup_relation = make_backup_relation(target_relation, backup_relation_type) -%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation) -%}
  {%- set preexisting_backup_relation = load_cached_relation(backup_relation) -%}

  {%- set identifier_pattern = '^[A-Za-z_][A-Za-z0-9_]*$' -%}
  {%- set source_pattern = '^[A-Za-z_][A-Za-z0-9_]*[.][A-Za-z_][A-Za-z0-9_]*$' -%}

  {% if bucket_key_column is none or bucket_key_column is not string or not modules.re.match(identifier_pattern, bucket_key_column|trim) %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_key_column is required and must be a bare column identifier '
        ~ '(letters, digits, underscore), got "' ~ bucket_key_column ~ '".'
    ) }}
  {% endif %}
  {% set bucket_key_column = bucket_key_column|trim %}
  {% if bucket_snapshot_column is none or bucket_snapshot_column is not string or not modules.re.match(identifier_pattern, bucket_snapshot_column|trim) %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_snapshot_column is required and must be a bare column identifier '
        ~ '(letters, digits, underscore), got "' ~ bucket_snapshot_column ~ '".'
    ) }}
  {% endif %}
  {% set bucket_snapshot_column = bucket_snapshot_column|trim %}
  {% if bucket_snapshot_column == bucket_key_column %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_snapshot_column must be a different column from bucket_key_column.'
    ) }}
  {% endif %}
  {% if bucket_source_table is none or bucket_source_table is not string or not modules.re.match(source_pattern, bucket_source_table|trim) %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_source_table is required and must have the form "database.table" '
        ~ 'with bare identifiers (letters, digits, underscore), got "' ~ bucket_source_table ~ '".'
    ) }}
  {% endif %}
  {% set bucket_source_table = bucket_source_table|trim %}
  {% if unique_key is none or unique_key != bucket_key_column %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: unique_key must be the single bucket_key_column "'
        ~ bucket_key_column ~ '", otherwise versions of one row split across buckets.'
    ) }}
  {% endif %}
  {% if rows_per_bucket is boolean or rows_per_bucket is not number or rows_per_bucket | int != rows_per_bucket or rows_per_bucket < 1 %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: rows_per_bucket must be a positive integer.'
    ) }}
  {% endif %}
  {% if on_concurrent_writes not in ('warn', 'error', 'ignore') %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: on_concurrent_writes must be one of "warn", "error", "ignore", got "'
        ~ on_concurrent_writes ~ '".'
    ) }}
  {% endif %}
  {% if inserts_only %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: inserts_only is not supported; incremental runs always use delete+insert.'
    ) }}
  {% endif %}
  {% set incremental_strategy = adapter.calculate_incremental_strategy(config.get('incremental_strategy')) %}
  {% if incremental_strategy != 'delete_insert' %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: only the delete_insert incremental strategy is supported, got "'
        ~ incremental_strategy ~ '". Set incremental_strategy="delete_insert"; it requires '
        ~ 'use_lw_deletes: true in the profile and a dbt user allowed to set '
        ~ 'allow_nondeterministic_mutations.'
    ) }}
  {% endif %}
  {% set incremental_predicates = config.get('predicates', []) or config.get('incremental_predicates', []) %}
  {% set partition_by = config.get('partition_by') %}
  {% do adapter.validate_incremental_strategy(incremental_strategy, incremental_predicates, unique_key, partition_by) %}

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
    {% set bucket_parts = bucket_source_table.split('.') %}
    {% set bucket_relation = adapter.get_relation(
        database=bucket_parts[0], schema=bucket_parts[0], identifier=bucket_parts[1]
    ) %}
    {% if bucket_relation is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
          ~ '" not found for model "' ~ model.name ~ '".'
      ) }}
    {% endif %}
    {% if bucket_relation.type != 'table' %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
          ~ '" must be a table, got type "' ~ bucket_relation.type ~ '".'
      ) }}
    {% endif %}
    {% set ns = namespace(
        bucket_dtype=none, bucket_wrapped=false, snapshot_dtype=none, snapshot_wrapped=false
    ) %}
    {% for col in adapter.get_columns_in_relation(bucket_relation) %}
      {#- ClickHouseColumn strips Nullable/LowCardinality from dtype into flags;
          data_type re-wraps them for the error messages, while the flags reject
          wrappers regardless of adapter data_type behaviour. -#}
      {% if col.name == bucket_key_column %}
        {% set ns.bucket_dtype = col.data_type %}
        {% set ns.bucket_wrapped = col.is_nullable or col.is_low_cardinality %}
      {% endif %}
      {% if col.name == bucket_snapshot_column %}
        {% set ns.snapshot_dtype = col.data_type %}
        {% set ns.snapshot_wrapped = col.is_nullable or col.is_low_cardinality %}
      {% endif %}
    {% endfor %}
    {% if ns.bucket_dtype is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_key_column "' ~ bucket_key_column
          ~ '" not found in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% if ns.bucket_wrapped %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_key_column "' ~ bucket_key_column
          ~ '" has unsupported type "' ~ ns.bucket_dtype
          ~ '"; expected a non-null UUID, signed integer or unsigned integer column.'
      ) }}
    {% elif ns.bucket_dtype == 'UUID' %}
      {% set bucket_key_type = 'uuid' %}
    {% elif ns.bucket_dtype in ('Int8', 'Int16', 'Int32', 'Int64', 'Int128', 'Int256') %}
      {% set bucket_key_type = 'int' %}
    {% elif ns.bucket_dtype in ('UInt8', 'UInt16', 'UInt32', 'UInt64', 'UInt128', 'UInt256') %}
      {% set bucket_key_type = 'uint' %}
    {% else %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_key_column "' ~ bucket_key_column
          ~ '" has unsupported type "' ~ ns.bucket_dtype
          ~ '"; expected a non-null UUID, signed integer or unsigned integer column.'
      ) }}
    {% endif %}
    {% if bucket_key_type == 'uuid' %}
      {% set bucket_lhs = 'reinterpretAsUInt64(' ~ bucket_key_column ~ ')' %}
    {% else %}
      {% set bucket_lhs = bucket_key_column %}
    {% endif %}
    {% if ns.snapshot_dtype is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" not found in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% if ns.snapshot_wrapped or not modules.re.match("^DateTime64[(]9(, *'[^']+')?[)]$", ns.snapshot_dtype) %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" has unsupported type "' ~ ns.snapshot_dtype
          ~ '"; it must be a non-null DateTime64(9) column.'
      ) }}
    {% endif %}
    {% set snapshot_tz = ns.snapshot_dtype.split("'")[1] if "'" in ns.snapshot_dtype else none %}
    {#- Every rebuild fills the intermediate relation and is published at the
        end, so readers never see a half-built table and a failed build leaves
        the target untouched. Publishing is a plain rename on the first run,
        an atomic exchange when the existing table supports it, or two renames
        otherwise (views and engines without atomic exchange report
        can_exchange=False). Bucket builds are pinned to the snapshot bound S0
        (max of bucket_snapshot_column, captured with the row count): every
        bucket reads only rows up to S0, so writes landing mid-build are
        excluded from all buckets and picked up by the next incremental run
        instead of slipping through silently. The snapshot column must be a
        non-null DateTime64(9); models must compare it with >= against the
        previous target maximum so a write stamped exactly S0 is recaptured. -#}
    {% set build_relation = intermediate_relation %}
    {% set need_swap = true %}
    {% set count_select = ['count() as row_count'] %}
    {% if bucket_key_type in ('int', 'uint') %}
      {% do count_select.append('countIf(' ~ bucket_key_column ~ ' < 0) as negative_key_count') %}
      {% set snapshot_idx = 2 %}
    {% else %}
      {% set snapshot_idx = 1 %}
    {% endif %}
    {% do count_select.append('toString(max(' ~ bucket_snapshot_column ~ ')) as snapshot_max') %}
    {% set count_sql %}select {{ count_select | join(', ') }} from {{ bucket_source_table }}{% endset %}
    {% set count_result = run_query(count_sql) %}
    {% set row_count = count_result.columns[0].values()[0] | int %}
    {% if bucket_key_type in ('int', 'uint') %}
      {% set negative_key_count = count_result.columns[1].values()[0] | int %}
      {% if negative_key_count > 0 %}
        {{ exceptions.raise_compiler_error(
            'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
            ~ '" has ' ~ negative_key_count ~ ' negative values in "' ~ bucket_key_column
            ~ '". Negative integer keys cannot be bucketed (the bucket maths uses '
            ~ 'modulo), so a full refresh would silently drop them. Use a '
            ~ 'non-negative key column.'
        ) }}
      {% endif %}
    {% endif %}
    {% set snapshot_str = count_result.columns[snapshot_idx].values()[0] %}
    {#- Defensive: a non-null DateTime64(9) column always yields a maximum (the
        epoch at the earliest, even for an empty table), and nullable or wrapped
        columns are rejected in the probe, so this guard is unreachable through
        the public contract (see integration-test-implementation F6). -#}
    {% if snapshot_str is none or snapshot_str|trim|length == 0 %}
      {% set snapshot_str = none %}
    {% endif %}
    {% if snapshot_str is none and row_count > 0 %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" has no usable maximum in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% if snapshot_str is none %}
      {% set snapshot_literal = none %}
    {% else %}
      {% set snapshot_literal = "toDateTime64('" ~ snapshot_str ~ "', 9" ~ (", '" ~ snapshot_tz ~ "'" if snapshot_tz else "") ~ ")" %}
    {% endif %}
    {% set bucket_count = ((row_count / rows_per_bucket) | round(0, 'ceil')) | int %}
    {{ log(
        'bucketed_incremental: row_count='
        ~ row_count
        ~ ' bucket_count='
        ~ bucket_count
        ~ ' snapshot_max='
        ~ (snapshot_str or 'n/a'),
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
            ~ i
            ~ ' and '
            ~ bucket_snapshot_column
            ~ ' <= '
            ~ snapshot_literal %}
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
    {% if snapshot_str is not none and on_concurrent_writes != 'ignore' %}
      {% set post_count_sql %}select max({{ bucket_snapshot_column }}) > {{ snapshot_literal }} as writes_detected from {{ bucket_source_table }}{% endset %}
      {% set post_count_result = run_query(post_count_sql) %}
      {% set writes_detected = post_count_result.columns[0].values()[0] %}
      {% if writes_detected %}
        {% if on_concurrent_writes == 'error' %}
          {{ exceptions.raise_compiler_error(
              'bucketed_incremental: concurrent writes to bucket_source_table "' ~ bucket_source_table
              ~ '" detected during the rebuild (snapshot bound ' ~ snapshot_str ~ ' exceeded). '
              ~ 'Re-run against a quiesced source; the previous table was left untouched.'
          ) }}
        {% else %}
          {{ log(
              'bucketed_incremental: WARNING: concurrent writes to bucket_source_table "'
              ~ bucket_source_table
              ~ '" detected during the rebuild; run an incremental afterwards to converge.',
              info=True,
          ) }}
        {% endif %}
      {% endif %}
    {% endif %}

  {% else %}
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
