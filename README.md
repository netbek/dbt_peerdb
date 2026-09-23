# dbt_peerdb

dbt helpers for PeerDB-replicated ClickHouse tables.

## Installation

1. Add the package to your `packages.yml`:

    ```yaml
    packages:
      - package: https://github.com/netbek/dbt_peerdb
        version: 0.0.10
    ```

2. Configure the package in your `dbt_project.yml`:

    ```yaml
    vars:
      dbt_peerdb_columns: [_peerdb_synced_at, _peerdb_is_deleted, _peerdb_version]
    ```

3. Run `dbt deps` to install the package.

## Macros

### bucketed_incremental materialization

The `bucketed_incremental` materialization builds very large ClickHouse
tables without a single monolithic query. A first run or `--full-refresh`
counts the source, splits it into key buckets of about `rows_per_bucket`
rows, and loads them one by one into a temporary table, which is then
published in one step — a rename on the first run, an atomic exchange
afterwards. Later runs take the standard `delete_insert` path: delete the
target rows the new batch replaces, then insert the new rows.

Every bucket reads only rows up to a captured snapshot bound, so a write
stamped after the bound is excluded from every bucket; a post-build check
then fails the run (`error`, the default), logs (`warn`), or stays quiet
(`ignore`) when the source moved meanwhile. Models select changed keys
with `snapshot >= high_watermark`, so the next incremental run recovers
those writes, including ties at the bound. The key column type is inferred
from the source (non-null UUID, signed or unsigned integers) and
`unique_key` must be that same single column.

Based on the [dbt-clickhouse `incremental` materialization](https://github.com/ClickHouse/dbt-clickhouse/blob/v1.10.2/dbt/include/clickhouse/macros/materializations/incremental/incremental.sql).
Details: [purpose and requirements](docs/bucketed_incremental/spec.md), [design](docs/bucketed_incremental/design.md).

What your model must do:

- Set `bucket_key_column` to the key column: a single bare identifier that
  exists in `bucket_source_table`, and the same column as `unique_key`, so
  every version of one row always lands in the same bucket.
- Set `bucket_source_table` to the `database.table` the model reads. The
  materialization counts it, probes its column types, and bounds every
  bucket by its snapshot maximum, so keep it in sync with the model.
- Set `bucket_snapshot_column` to a non-null `DateTime64(9)` column of the
  source (e.g. `_peerdb_synced_at`), different from the key column. The
  materialization captures its maximum before building and reads only rows
  up to that bound.
- Set `rows_per_bucket` (default 1000000, a positive whole number) to size
  the buckets: the bucket count is the source row count divided by this
  number, rounded up. Lower it if a single bucket exhausts memory.
- Call `dbt_peerdb.is_incremental()` to detect incremental runs, as in the
  example. dbt resolves a plain `is_incremental()` from the root project or
  the adapter's global macros, which do not recognise `bucketed_incremental`,
  so the plain call compiles the full-history branch on every run.

Example model:

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
    from {{ source('my_database', 'my_table') }}
    -- `>=`, not `>`: recovers a write stamped exactly at the snapshot bound.
    -- The watermark batch is re-read on every incremental run.
    where _peerdb_synced_at >= (select high_watermark from watermark)
),
{% endif %}
raw_versions as (
    select id, payload, _peerdb_synced_at, _peerdb_is_deleted, _peerdb_version
    from {{ source('my_database', 'my_table') }}
    {% if dbt_peerdb.is_incremental() %}
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

What the query does:

- On the first run or `--full-refresh`, the full-history branch runs once
  per bucket. Each pass fills `-- __BUCKET_PREDICATE__` with its own key
  slice and snapshot bound, then collapses each key to one row — latest
  version wins. Tombstone versions (`_peerdb_is_deleted = 1`) are kept as
  rows like any other version.
- On incremental runs, the model reads the high watermark (the target's
  maximum snapshot), re-reads every version of each key stamped at or
  after it, dedupes to one row per key, and the delete+insert step
  replaces those keys in the target. The `>=` comparison recaptures a
  write stamped exactly at the previous bound; keep it if you adapt this
  example. Tombstones replace their keys the same way, so deletes arrive
  as rows and downstream models filter them.

`_peerdb_synced_at` is the high-watermark column PeerDB maintains on
replicated tables; for non-PeerDB sources use your own updated-at column,
declared non-null `DateTime64(9)`.

## Development

### Prerequisites

1. Clone the repo:

    ```shell
    git clone git@github.com:netbek/dbt_peerdb.git
    ```

2. Install Mise and add activation to `~/.bashrc`, e.g.

    ```shell
    curl -fsSL https://github.com/jdx/mise/releases/download/v2026.7.13/install.sh | sh
    ```

    See [other installation methods](https://mise.en.dev/installing-mise.html).

3. Trust `mise.toml`:

    ```shell
    mise trust
    ```

4. Run `make install` to install Node dependencies, Python dependencies, pre-commit hooks, agent skills, dbt packages for tests, ClickHouse config, and pinned vendor sources:

  ```shell
  make install
  ```

### Testing

### Release

1. Run `make bump-version [major|minor|patch]`. This bumps `pyproject.toml`, syncs `package.json`, `dbt_project.yml`, and the `packages.yml` pin in this README, then commits.
2. Push the commit.
3. Check the tree is clean, then run `make create-release`.

## License

Copyright (c) 2024 Hein Bekker. Licensed under the Apache License, version 2.
