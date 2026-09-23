# bucketed_incremental Specification

## Purpose

The `bucketed_incremental` materialization builds and maintains large ClickHouse tables that a change-data-capture process writes to continuously. A full refresh loads the source in sequential key buckets into an intermediate relation and publishes the finished table in one step, so readers never see a half-built table and a failed build leaves the previous contents in place. Incremental runs replace only the keys that the source touched, using the adapter's delete+insert path. A required snapshot column pins every bucket to a captured bound, and a post-build check fails a rebuild when writes land mid-build, so concurrent writers cannot slip through unnoticed.

Terms (`bucket_key_column`, snapshot column, `S0`, `W`, high watermark, marker) are defined once in [design Terminology](design.md#terminology); columns name data while watermarks are thresholds derived from the snapshot column.

## Requirements

### Requirement: Incremental Detection

`dbt_peerdb.is_incremental()` SHALL treat `bucketed_incremental` like the built-in incremental materializations: it reports true only when the target relation is a table, the model uses an incremental materialization, and the run is not a full refresh.

#### Scenario: First run
- **WHEN** the target relation does not exist
- **THEN** `dbt_peerdb.is_incremental()` returns false

#### Scenario: Later run
- **WHEN** the target is a table, the model is materialized as `bucketed_incremental`, and the run is not a full refresh
- **THEN** `dbt_peerdb.is_incremental()` returns true

#### Scenario: Full refresh
- **WHEN** the run requests a full refresh, for example with `--full-refresh`
- **THEN** `dbt_peerdb.is_incremental()` returns false
- **AND** the materialization uses the bucketed full-refresh path

#### Scenario: Package-qualified call
- **WHEN** a model calls `is_incremental()` without the package qualifier
- **THEN** the call resolves to the adapter or global macro, which do not recognise `bucketed_incremental`, and returns false on every run
- **AND** models SHALL detect incremental runs with `dbt_peerdb.is_incremental()`

### Requirement: Bucketed Full Refresh

A first run or any full refresh SHALL build the table in sequential key buckets and SHALL publish it only after every bucket succeeds.

#### Scenario: Rebuild with an existing table
- **WHEN** the target table exists and the run is a full refresh
- **THEN** every bucket loads into an intermediate relation
- **AND** the target stays readable with its previous contents until the publish step

#### Scenario: Bucket failure
- **WHEN** any bucket statement fails
- **THEN** the build stops
- **AND** the target keeps its previous contents

#### Scenario: First run
- **WHEN** no target relation exists
- **THEN** the full refresh still loads the buckets into the intermediate relation
- **AND** the publish step renames it to the target

### Requirement: Bucket Predicate Marker

The model SQL for a full refresh SHALL contain the marker `-- __BUCKET_PREDICATE__` exactly once, in the full-history branch. The marker SHALL stand where a `WHERE` clause belongs: the full-history branch SHALL NOT contain a `WHERE` before the marker, and additional full-refresh conditions SHALL follow the marker joined with `AND`. The materialization SHALL replace the marker with the predicate of the current bucket on every bucket pass.

#### Scenario: Marker replaced
- **WHEN** a bucket pass runs
- **THEN** the marker is replaced by a `where` clause of the form `<key expression> % <bucket count> = <bucket index>`
- **AND** the clause also carries `<snapshot column> <= <snapshot bound>` when pinning applies
- **AND** the replacement itself begins with `where`, so the full-history branch holds no `WHERE` before the marker

#### Scenario: Marker missing or duplicated
- **WHEN** the SQL for a full refresh contains no marker or more than one marker
- **THEN** the run stops with a compiler error that names the marker and the number of occurrences found
- **AND** no bucket statement runs

#### Scenario: Incremental run
- **WHEN** the run is incremental
- **THEN** the compiled statement does not contain the marker, and the materialization does not check for it

### Requirement: Bucket Sizing

The materialization SHALL size buckets from `rows_per_bucket`, a positive integer of at least 1 that defaults to 100,000, and the row count of `bucket_source_table`.

#### Scenario: Bucket count
- **WHEN** the row count of `bucket_source_table` is `R` and `rows_per_bucket` is `B`
- **THEN** the materialization runs `ceil(R / B)` bucket passes

#### Scenario: Invalid bucket size
- **WHEN** `rows_per_bucket` is not a positive integer, or is a boolean
- **THEN** the run stops with a compiler error before any bucket runs

#### Scenario: Run log
- **WHEN** the count query completes
- **THEN** the materialization logs the row count, the bucket count and the captured snapshot maximum

### Requirement: Empty Source

The materialization SHALL treat a source with no rows as an empty build, not an error.

#### Scenario: Empty source
- **WHEN** `bucket_source_table` contains no rows
- **THEN** the materialization builds an empty table with a `where 1 = 0` predicate in place of the marker
- **AND** it publishes that table through the normal publish step

### Requirement: Configuration Validation

The materialization SHALL validate its configuration on every run, before any database work and before pre-hooks run.

#### Scenario: Bucket identifier
- **WHEN** `bucket_key_column` or `bucket_snapshot_column` is missing or is not a bare identifier of letters, digits and underscores
- **THEN** the run stops with a compiler error

#### Scenario: Source table identifier
- **WHEN** `bucket_source_table` is missing or does not have the form `database.table` with bare identifiers
- **THEN** the run stops with a compiler error

#### Scenario: Snapshot column differs from key column
- **WHEN** `bucket_snapshot_column` is the same column as `bucket_key_column`
- **THEN** the run stops with a compiler error

#### Scenario: Unique key
- **WHEN** `unique_key` is not the single `bucket_key_column`
- **THEN** the run stops with a compiler error

#### Scenario: Concurrent-write response
- **WHEN** `on_concurrent_writes` is not one of `warn`, `error` or `ignore`
- **THEN** the run stops with a compiler error

#### Scenario: Inserts only
- **WHEN** `inserts_only` is true
- **THEN** the run stops with a compiler error

### Requirement: Strategy Enforcement

The materialization SHALL use the adapter's `delete_insert` incremental strategy on every run and SHALL reject every other strategy.

#### Scenario: Resolved strategy
- **WHEN** the adapter resolves `incremental_strategy` to something other than `delete_insert`, including the `legacy` default when the profile does not opt in to lightweight deletes
- **THEN** the run stops with a compiler error that names the resolved strategy

#### Scenario: Strategy arguments
- **WHEN** the resolved strategy is `delete_insert`
- **THEN** the materialization calls `adapter.validate_incremental_strategy` with the strategy, the incremental predicates, `unique_key` and `partition_by`
- **AND** a validation failure from the adapter stops the run

### Requirement: Key Column Contract

The materialization SHALL bucket on one column that never changes and identifies one row, SHALL infer the key type from the source column, and SHALL reject sources that break the contract.

#### Scenario: UUID key
- **WHEN** the source `bucket_key_column` is a non-null `UUID` column
- **THEN** the bucket expression is `reinterpretAsUInt64(bucket_key_column)`

#### Scenario: Integer key
- **WHEN** the source `bucket_key_column` is a non-null `Int8`, `Int16`, `Int32`, `Int64`, `Int128`, `Int256`, `UInt8`, `UInt16`, `UInt32`, `UInt64`, `UInt128` or `UInt256` column
- **THEN** the bucket expression is `bucket_key_column`

#### Scenario: Unsupported key type
- **WHEN** the source key column is nullable, wrapped, or of any other type
- **THEN** the run stops with a compiler error that names the column and its type

#### Scenario: Negative integer key
- **WHEN** the source contains negative values in an integer `bucket_key_column`
- **THEN** the run stops with a compiler error
- **AND** the error reports how many negative values the source holds

#### Scenario: Missing key column
- **WHEN** `bucket_source_table` does not contain `bucket_key_column`
- **THEN** the run stops with a compiler error

### Requirement: Source Table Contract

The materialization SHALL read counts and type information from `bucket_source_table`, and that relation SHALL exist as a table at run time.

#### Scenario: Source missing
- **WHEN** `bucket_source_table` does not exist
- **THEN** the run stops with a compiler error that names the model and the table

#### Scenario: Source is not a table
- **WHEN** `bucket_source_table` exists but is a view or another non-table relation
- **THEN** the run stops with a compiler error that names the relation type

#### Scenario: Source missing the snapshot column
- **WHEN** `bucket_source_table` does not contain `bucket_snapshot_column`
- **THEN** the run stops with a compiler error

### Requirement: Snapshot Column Contract

The materialization SHALL pin every full refresh to a snapshot bound captured before the first bucket.

Note: recovery through the bound assumes per-key-monotonic stamps. PeerDB `_peerdb_synced_at` provides this on a single node (destination-stamped at normalize time); own updated-at columns meet this contract only under quiesced rebuilds, since late, backfilled, or corrected source rows routinely carry older stamps.

#### Scenario: Snapshot type
- **WHEN** the source `bucket_snapshot_column` is not a non-null `DateTime64(9)` column with an optional timezone
- **THEN** the run stops with a compiler error that names the column and its type

#### Scenario: Snapshot bound
- **WHEN** the count query returns a snapshot maximum `S0`
- **THEN** every bucket pass reads only rows with `<snapshot column> <= S0`

#### Scenario: Timezone
- **WHEN** the snapshot column declares a timezone
- **THEN** the bound literal keeps that timezone

### Requirement: Concurrent-Write Detection

After the bucket loop and before the publish step, the materialization SHALL compare the source maximum snapshot with the captured bound, unless `on_concurrent_writes` is `ignore` or the captured maximum is empty (`row_count == 0`; an epoch `max` on an empty source counts as empty, cf. F6 in `integration-test-implementation.md`). Detection fires only when the source maximum exceeds `S0`; source changes that do not raise the maximum (truncate, TTL or retention deletes) are not detected.

#### Scenario: Error response
- **WHEN** the source maximum exceeds `S0` and `on_concurrent_writes` is `error`, the default
- **THEN** the run stops with a compiler error
- **AND** the target keeps its previous contents

#### Scenario: Warn response
- **WHEN** the source maximum exceeds `S0` and `on_concurrent_writes` is `warn`
- **THEN** the materialization logs a warning
- **AND** the publish step proceeds

#### Scenario: Ignore response
- **WHEN** `on_concurrent_writes` is `ignore`
- **THEN** the materialization runs no detection query

#### Scenario: Detection query
- **WHEN** detection runs
- **THEN** it costs one additional `max()` scan of `bucket_source_table`

### Requirement: Atomic Publication

The materialization SHALL publish a finished build in one step and SHALL leave the target untouched when a build fails.

#### Scenario: First run
- **WHEN** no target relation exists and the build succeeds
- **THEN** the materialization renames the intermediate relation to the target

#### Scenario: Exchange supported
- **WHEN** the existing relation reports `can_exchange`
- **THEN** the materialization renames the intermediate relation to the backup name
- **AND** exchanges the backup and the target atomically
- **AND** drops the backup after the commit

#### Scenario: Exchange unsupported
- **WHEN** the existing relation does not report `can_exchange`
- **THEN** the materialization renames the target to the backup name
- **AND** renames the intermediate relation to the target
- **AND** drops the backup after the commit

#### Scenario: Preexisting temporary relations
- **WHEN** a relation with the intermediate or backup name already exists
- **THEN** the materialization drops it before the build

### Requirement: Incremental Maintenance

On a run where the target table exists and no full refresh is requested, the materialization SHALL replace changed keys with the adapter's delete+insert step instead of rebuilding buckets.

#### Scenario: Delete and insert
- **WHEN** an incremental run starts
- **THEN** the materialization creates a temporary table from the model SQL
- **AND** deletes the target rows whose `unique_key` values appear in that table
- **AND** inserts the temporary table into the target

#### Scenario: Incremental predicates
- **WHEN** the model sets `predicates` or `incremental_predicates`
- **THEN** the materialization passes them to the adapter's delete+insert step

#### Scenario: Schema change
- **WHEN** `on_schema_change` is not `ignore`, for example `append_new_columns`
- **THEN** the materialization checks for column changes and applies them before the delete+insert step

### Requirement: Model Contract

Models SHALL satisfy the parts of the design that the materialization cannot inspect.

#### Scenario: One row per key
- **WHEN** the full-history branch returns several rows for one key
- **THEN** every version of that key lands in the same bucket
- **AND** the target keeps the duplicate rows, so models SHALL collapse each key to one row before selecting

#### Scenario: Tie-safe watermark
- **WHEN** the incremental branch selects changed keys with `<snapshot column> >= <high watermark>`, where the watermark is the target's maximum snapshot
- **THEN** a write stamped exactly at the previous bound is re-selected on the next incremental run
- **NOTE** a write stamped below the watermark is not re-selected by any predicate the macro can enforce; under PeerDB this needs skew, step-back, or manual writes, while under own updated-at columns it is routine late/backfilled data (see provenance note above)

#### Scenario: Strict watermark
- **WHEN** a model uses `>` instead of `>=`
- **THEN** the materialization cannot enforce the contract
- **AND** a write stamped exactly at `S0` can be missed permanently

#### Scenario: All versions of a touched key
- **WHEN** the incremental branch selects only the changed row for a key
- **THEN** a later version that already exists in the source is not re-read
- **AND** the target can keep a stale row, so models SHALL re-read every version of each key they select
