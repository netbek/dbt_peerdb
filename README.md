# dbt_peerdb

## Installation

1. Add the package to your `packages.yml`:

    ```yaml
    packages:
      - package: https://github.com/netbek/dbt_peerdb
        version: 0.0.7
    ```

2. Configure the package in your `dbt_project.yml`:

    ```yaml
    vars:
      dbt_peerdb_columns: [_peerdb_synced_at, _peerdb_is_deleted, _peerdb_version]
    ```

3. Run `dbt deps` to install the package.

## Macros

### bucketed_incremental materialization

A gentler way to build very large tables.

It works like a normal incremental model (https://docs.getdbt.com/docs/build/incremental-models):

- First run, or any run with `--full-refresh`: dbt builds the whole table
  from all rows of the source. This materialization splits that big build
  into small buckets by key (about `rows_per_bucket` rows each), loads
  them one by one into a temporary table, then swaps it into place. Readers
  never see a half-built table. The materialization counts the source table
  itself to decide how many buckets it needs.
- Incremental runs (every run after that): dbt transforms only new or
  changed rows since the last run and inserts them into the target table.
  Here that means the standard `delete+insert` step: delete the rows in the
  target table that the new batch replaces, then insert the new rows.

It fits any source where one column never changes and identifies one row
(a unique key), of type uuid or integer. Negative integer keys do not work
(the bucket maths uses modulo). Only the `delete_insert` strategy is
supported: `inserts_only` and any other `incremental_strategy` stop with an
error instead of silently doing the wrong thing.

What your model must do:

- Put the line `-- __BUCKET_PREDICATE__` exactly where the bucket filter
  belongs in the full-history branch of your SQL. Each full-refresh pass
  fills that line in with its own bucket filter.
- Set `unique_key` to the same single column as `bucket_column`, so every
  version of one row always lands in the same bucket. Otherwise duplicates
  slip through unnoticed.
- In the full-history branch, collapse each key down to one row (dedupe to
  the `unique_key` grain): every pass re-reads all versions of the keys in
  its bucket.

If `bucket_source_table` is empty, it builds an empty table (and swaps it in)
instead of failing.

Bucket size is set by `rows_per_bucket` (default 1000000, must be a
positive whole number). The bucket count is the source table row count
divided by this number, rounded up. Lower it if a single bucket exhausts memory;
raise it for small tables to avoid a flurry of tiny buckets.

UUID example model config:

```sql
{{ config(
    materialized="bucketed_incremental",
    unique_key="uuid",
    order_by="uuid",
    bucket_column="uuid",
    bucket_type="uuid",
    bucket_source_table="my_database.my_table"
) }}
```

Integer example model config:

```sql
{{ config(
    materialized="bucketed_incremental",
    unique_key="id",
    order_by="id",
    bucket_column="id",
    bucket_type="int",
    bucket_source_table="my_database.my_table"
) }}
```

Full example model (integer key):

```sql
{{ config(
    materialized="bucketed_incremental",
    engine="MergeTree()",
    unique_key="id",
    order_by="id",
    bucket_column="id",
    bucket_type="int",
    bucket_source_table="my_database.my_table",
    rows_per_bucket=2000000
) }}

with
{% if is_incremental() %}
watermark as (
    select coalesce(
        max(_peerdb_synced_at),
        toDateTime64('1970-01-01 00:00:00.000000000', 9)
    ) as high_watermark
    from {{ this }}
),
changed_keys as (
    select distinct id
    from {{ source('my_database', 'my_table') }}
    where _peerdb_synced_at > (select high_watermark from watermark)
),
{% endif %}
raw_versions as (
    select id, payload, _peerdb_synced_at, _peerdb_is_deleted, _peerdb_version
    from {{ source('my_database', 'my_table') }}
    {% if is_incremental() %}
    -- All versions of each touched key, so late versions dedupe correctly.
    where id in (select id from changed_keys)
    {% else %}
    -- __BUCKET_PREDICATE__
    {% endif %}
),
deduped as (
    select id, payload, _peerdb_synced_at, _peerdb_is_deleted, _peerdb_version
    from raw_versions
    order by _peerdb_version desc, _peerdb_synced_at desc
    limit 1 by id
)
select * from deduped
```

On incremental runs this re-reads every version of each touched id; the
`delete+insert` step then replaces those ids in the target table.
`_peerdb_synced_at` is the high-watermark column PeerDB maintains on
replicated tables; for non-PeerDB sources use your own updated-at column.
Tombstones (`_peerdb_is_deleted = 1`) are kept; downstream models filter them.

## License

Copyright (c) 2024 Hein Bekker. Licensed under the Apache License, version 2.
