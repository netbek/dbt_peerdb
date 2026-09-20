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
  from all rows of the source up to the captured snapshot bound (see
  below). This materialization splits that big build
  into small buckets by key (about `rows_per_bucket` rows each), loads
  them one by one into a temporary table, then publishes it in one step: a
  rename on the first run; an atomic exchange afterwards (two renames for
  views and engines without atomic exchange). Readers never see a
  half-built table, and a failed build leaves the old contents untouched.
  The materialization counts the source table itself to decide how many
  buckets it needs, and each bucket re-reads the source, so a rebuild costs
  about one source scan per bucket.
- Incremental runs (every run after that): dbt transforms only new or
  changed rows since the last run and inserts them into the target table.
  Here that means the standard `delete+insert` step: delete the rows in the
  target table that the new batch replaces, then insert the new rows.

It fits any source where one column never changes and identifies one row
(a unique key). The materialization reads the key column type from the
source table itself: a non-null `UUID` buckets by `reinterpretAsUInt64`,
a signed `Int8`-`Int256` or an unsigned `UInt8`-`UInt256` buckets by
value; nullable, wrapped or other key columns stop with an error. The
bucket maths uses modulo, so integer keys
must be non-negative: a full refresh that finds negative keys stops with
an error instead of silently dropping those rows. Only the `delete_insert`
strategy is supported, and that is enforced on every run: `inserts_only`,
an unresolved strategy, or any other `incremental_strategy` stops with an
error instead of silently doing the wrong thing. Set
`incremental_strategy="delete_insert"` explicitly, and set
`use_lw_deletes: true` in your profile: the adapter resolves the default
to `delete_insert` only when the profile opts in and the server allows
`allow_nondeterministic_mutations` (needed for lightweight deletes);
otherwise its default is `legacy`, which this materialization rejects.

Rebuilds are pinned to a snapshot so concurrent writes cannot slip
through silently. Set `bucket_snapshot_column` to a non-null
`DateTime64(9)` column of the source (required, e.g. `_peerdb_synced_at`;
any other type stops with an error): the materialization captures its
maximum (S0) before building, each bucket reads only rows up to that
bound, and every write landing mid-build carries a stamp at or above S0.
`on_concurrent_writes` controls the response when writes newer than S0
are detected after the loop: `error` (the default) fails the build before
publishing, `warn` logs, `ignore` stays quiet and skips the check. The
detector costs one additional `max()` scan of the source.

For the next incremental run to pick those writes up, the model's
watermark predicate must use `>=`, not `>`: the previous build's maximum
is at most S0, so `_peerdb_synced_at >= high_watermark` recaptures every
mid-build write, including one stamped exactly S0. A model that uses `>`
can permanently miss a write stamped exactly S0, so this materialization
requires the `>=` pattern. The cost is that the batch at the watermark is
re-read on every incremental run.

What your model must do:

- Put the line `-- __BUCKET_PREDICATE__` exactly where the bucket filter
  belongs in the full-history branch of your SQL. Each bucket pass fills
  that line in with its own bucket filter.
- Set `unique_key` to the same single column as `bucket_key_column`, so every
  version of one row always lands in the same bucket. Otherwise duplicates
  slip through unnoticed.
- In the full-history branch, collapse each key down to one row (dedupe to
  the `unique_key` grain): every pass re-reads all versions of the keys in
  its bucket.
- In the incremental branch, select changed keys with
  `snapshot_column >= high_watermark` (see above); a strict `>` is not
  tie-safe.

If `bucket_source_table` has no rows, it builds an empty table (and
publishes it) instead of failing; leaving `bucket_source_table` unset is
an error.

Bucket size is set by `rows_per_bucket` (default 1000000, must be a
positive whole number). The bucket count is the row count of
`bucket_source_table` divided by this number, rounded up, so keep that
table in sync with what the model reads, or buckets end up mis-sized.
Lower it if a single bucket exhausts memory; raise it for small tables to
avoid a flurry of tiny buckets. Each bucket re-reads `bucket_source_table`
at a different moment, but the snapshot bound plus the `>=` watermark
contract keeps concurrent writes from being lost. Two residual cases
remain: a source that physically deletes rows needs a quiesced rebuild (a
missed delete leaves a stale row that no incremental run clears), and a
write carrying a stamp below S0 after its bucket has been read (clock
skew or backdated data) is not re-selected.

UUID example model config:

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy="delete_insert",
    unique_key="uuid",
    order_by="uuid",
    bucket_key_column="uuid",
    bucket_source_table="my_database.my_table",
    bucket_snapshot_column="_peerdb_synced_at"
) }}
```

Integer example model config:

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy="delete_insert",
    unique_key="id",
    order_by="id",
    bucket_key_column="id",
    bucket_source_table="my_database.my_table",
    bucket_snapshot_column="_peerdb_synced_at"
) }}
```

Full example model (integer key):

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy="delete_insert",
    engine="MergeTree()",
    unique_key="id",
    order_by="id",
    bucket_key_column="id",
    bucket_source_table="my_database.my_table",
    bucket_snapshot_column="_peerdb_synced_at",
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
    -- `>=`, not `>`: recovers a write stamped exactly at the snapshot bound.
    -- The watermark batch is re-read on every incremental run.
    where _peerdb_synced_at >= (select high_watermark from watermark)
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
`delete+insert` step then replaces those ids in the target table. The `>=`
comparison is required by the snapshot guarantee (see above); keep it if
you adapt this example. `_peerdb_synced_at` is the high-watermark column
PeerDB maintains on replicated tables; for non-PeerDB sources use your own
updated-at column, declared non-null `DateTime64(9)`. Tombstones
(`_peerdb_is_deleted = 1`) are kept; downstream models filter them.

## License

Copyright (c) 2024 Hein Bekker. Licensed under the Apache License, version 2.
