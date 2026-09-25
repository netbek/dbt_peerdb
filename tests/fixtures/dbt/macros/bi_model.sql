{# Shared model body for the bucketed_incremental integration tests.

   Tests shape default.bi_source per scenario; source-branch models set
   bucket_source=['bi', 'bi_source'] and read the default source, ref-branch
   models set bucket_ref and pass the matching ref() into bi_model_sql. The
   model body owns the relation call, so the DAG edge is registered at parse
   time (dbt's parse-mode config.get returns '', so the macro cannot branch on
   config itself). watermark_operator models the documented >= contract (use
   '>' to model the strict-watermark mistake). sleep_seconds slows the
   full-refresh branch so a late writer can race the build; it also pins
   max_threads=1 so the sleeps serialize. guard_expression is appended to the
   output so a specific row can fail a bucket on demand. detection_flags
   appends the package-qualified and plain is_incremental() results so a test
   can pin which call detects bucketed_incremental. #}

{% macro bi_model_sql(watermark_operator='>=', sleep_seconds=none, include_marker=true, guard_expression=none, detection_flags=false, relation=none) %}
{% set source_relation = relation if relation is not none else source('bi', 'bi_source') %}
with
{% if dbt_peerdb.is_incremental() %}
watermark as (
    select coalesce(
        max(_peerdb_synced_at),
        toDateTime64('1970-01-01 00:00:00.000000000', 9)
    ) as high_watermark
    from {{ this }}
),
changed_keys as (
    select distinct id
    from {{ source_relation }}
    where _peerdb_synced_at {{ watermark_operator }} (select high_watermark from watermark)
),
{% endif %}
raw_versions as (
    select id, payload, region, _peerdb_synced_at, _peerdb_is_deleted, _peerdb_version
    from {{ source_relation }}
    {% if dbt_peerdb.is_incremental() %}
    where id in (select id from changed_keys)
    {% else %}
    {%- if include_marker %}
    -- __BUCKET_PREDICATE__
    {%- endif %}
    {%- if sleep_seconds is not none %}
    and sleepEachRow({{ sleep_seconds }}) = 0
    {%- endif %}
    {% endif %}
),
deduped as (
    select id, payload, region, _peerdb_synced_at, _peerdb_is_deleted, _peerdb_version
    from raw_versions
    order by _peerdb_version desc, _peerdb_synced_at desc
    limit 1 by id
)
select *{% if guard_expression is not none %}, {{ guard_expression }} as _guard{% endif %}
{%- if detection_flags %},
    {{ 1 if dbt_peerdb.is_incremental() else 0 }} as qualified_incremental,
    {{ 1 if is_incremental() else 0 }} as plain_incremental
{%- endif %}
from deduped
{% if sleep_seconds is not none %}settings max_threads=1{% endif %}
{% endmacro %}
