# bucketed_incremental Specification

> Status: frozen v2 · approved 2026-10-02

## Purpose

The `bucketed_incremental` materialization builds and maintains large ClickHouse tables that a change-data-capture process writes to continuously. A full refresh loads the source in sequential key buckets into an intermediate relation and publishes the finished table in one step, so readers never see a half-built table and a failed build leaves the previous contents in place. Integer keys partition by modulo; UUID keys partition into contiguous key ranges, so a source sorted by the key reads only each bucket's range. Incremental runs replace only the keys that the source touched, using the adapter's delete+insert path. A required snapshot column pins every bucket to a captured bound, and a post-build check fails a rebuild when writes land mid-build, so concurrent writers cannot slip through unnoticed.

Terms (`bucket_key_column`, `bucket_ref`, `bucket_source`, snapshot column, `S0`, `W`, high watermark, marker, key bounds, step, remainder, boundary, range) are defined once in [design Terminology](design.md#terminology); columns name data while watermarks are thresholds derived from the snapshot column.

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
- **THEN** the marker is replaced by a `where` clause whose key form depends on the key type (see the two scenarios below)
- **AND** the clause also carries `<snapshot column> <= <snapshot bound>` when pinning applies
- **AND** the replacement itself begins with `where`, so the full-history branch holds no `WHERE` before the marker

#### Scenario: Integer predicate
- **WHEN** the key is a signed or unsigned integer
- **THEN** the predicate is `<key> % <bucket count> = <bucket index>`

#### Scenario: UUID range predicate
- **WHEN** the key is a UUID
- **THEN** the predicate is `<key> >= <lower> and <key> < <upper>`
- **AND** the last pass uses `<= <upper>` so the maximum key is included

#### Scenario: Marker missing or duplicated
- **WHEN** the SQL for a full refresh contains no marker or more than one marker
- **THEN** the run stops with a compiler error that names the marker and the number of occurrences found
- **AND** no bucket statement runs

#### Scenario: Incremental run
- **WHEN** the run is incremental
- **THEN** the compiled statement does not contain the marker, and the materialization does not check for it

### Requirement: Bucket Sizing

The materialization SHALL size buckets from `rows_per_bucket`, a positive integer of at least 1 that defaults to 100,000, and the row count of the resolved bucket relation.

#### Scenario: Bucket count
- **WHEN** the row count of the resolved bucket relation is `R` and `rows_per_bucket` is `B`
- **THEN** the materialization runs `ceil(R / B)` bucket passes

#### Scenario: Invalid bucket size
- **WHEN** `rows_per_bucket` is not a positive integer, or is a boolean
- **THEN** the run stops with a compiler error before any bucket runs

#### Scenario: Run log
- **WHEN** the count query completes
- **THEN** the materialization logs the row count, the bucket count and the captured snapshot maximum
- **AND** a UUID run also logs the key bounds and the step

### Requirement: Empty Source

The materialization SHALL treat a source with no rows as an empty build, not an error.

#### Scenario: Empty source
- **WHEN** the resolved bucket relation contains no rows
- **THEN** the materialization builds an empty table with a `where 1 = 0` predicate in place of the marker
- **AND** a UUID run computes no key bounds and runs no boundary query
- **AND** it publishes that table through the normal publish step

### Requirement: Configuration Validation

The materialization SHALL validate its configuration on every run, before any database work and before pre-hooks run.

#### Scenario: Bucket identifier
- **WHEN** `bucket_key_column` or `bucket_snapshot_column` is missing or is not a bare identifier of letters, digits and underscores
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

### Requirement: Relation Key Selection

The materialization SHALL accept `bucket_ref` and `bucket_source`, SHALL require exactly one of them on every run, and SHALL not read any other source-identity key.

#### Scenario: Exactly one set
- **WHEN** a run starts with both `bucket_ref` and `bucket_source` set, or with neither set
- **THEN** the run stops with a compiler error that names both keys
- **AND** no database work runs and no pre-hook runs

#### Scenario: Other keys ignored
- **WHEN** a model sets a key other than `bucket_ref` or `bucket_source`
- **THEN** the materialization ignores it

### Requirement: `bucket_ref` Shape

`bucket_ref` SHALL be a list or tuple of one or two bare identifiers, matching the arguments of `ref()`.

#### Scenario: Accepted forms
- **WHEN** `bucket_ref` is `["model_name"]`
- **THEN** the materialization resolves `ref("model_name")`
- **WHEN** `bucket_ref` is `["package_name", "model_name"]`
- **THEN** the materialization resolves `ref("package_name", "model_name")`

#### Scenario: Rejected forms
- **WHEN** `bucket_ref` is set and is not a list or tuple, or has a length other than 1 or 2, or holds any element that is not a string matching `^[A-Za-z_][A-Za-z0-9_]*$`
- **THEN** the run stops with a compiler error that names `bucket_ref` and the value received

#### Scenario: Versioned refs
- **WHEN** the bucket relation is a specific model version
- **THEN** `bucket_ref` cannot express it: `ref()` takes `version` as a keyword argument, and the list has no slot for it
- **AND** `bucket_ref` resolves the target model's latest version

### Requirement: `bucket_source` Shape

`bucket_source` SHALL be a list or tuple of exactly two bare identifiers, matching the arguments of `source()`.

#### Scenario: Accepted form
- **WHEN** `bucket_source` is `["source_name", "table_name"]`
- **THEN** the materialization resolves `source("source_name", "table_name")`

#### Scenario: Rejected forms
- **WHEN** `bucket_source` is set and is not a list or tuple, or has a length other than 2, or holds any element that is not a string matching `^[A-Za-z_][A-Za-z0-9_]*$`
- **THEN** the run stops with a compiler error that names `bucket_source` and the value received

### Requirement: Bucket Relation Resolution

The materialization SHALL resolve the bucket relation by calling the matching function with the configured elements, SHALL keep the returned Relation object, and SHALL derive database, schema, and identifier from that object. It SHALL NOT split strings or assume schema equals database.

#### Scenario: Relation fields
- **WHEN** either branch resolves
- **THEN** the probe calls `adapter.get_relation` with `database=bucket_relation.database`, `schema=bucket_relation.schema`, and `identifier=bucket_relation.identifier`

#### Scenario: Resolution timing
- **WHEN** the run is incremental
- **THEN** the materialization does not resolve the bucket relation, because the incremental path reads only the target and the model SQL
- **AND** the exclusivity and shape checks still run before any database work

#### Scenario: Resolution failure
- **WHEN** `ref()` or `source()` cannot resolve the target, for example an unknown model or package, or an undeclared source
- **THEN** dbt raises its own resolution error and the run stops; the materialization does not catch or rewrite it

### Requirement: Relation-Rendered SQL

Count and detection statements SHALL read from the resolved Relation object, not from an interpolated string.

#### Scenario: Count and detection
- **WHEN** the count query or the `max(snapshot) > S0` detection query runs
- **THEN** its `FROM` clause renders the resolved relation (correct quoting, database, and schema per target)

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
- **THEN** the bucket predicate compares the key column directly against constant UUID range bounds (see UUID Key Range Partition)

#### Scenario: Integer key
- **WHEN** the source `bucket_key_column` is a non-null `Int8`, `Int16`, `Int32`, `Int64`, `Int128`, `Int256`, `UInt8`, `UInt16`, `UInt32`, `UInt64`, `UInt128` or `UInt256` column
- **THEN** the bucket predicate is `<key> % <bucket count> = <bucket index>`

#### Scenario: Unsupported key type
- **WHEN** the source key column is nullable, wrapped, or of any other type
- **THEN** the run stops with a compiler error that names the column and its type

#### Scenario: Negative integer key
- **WHEN** the source contains negative values in an integer `bucket_key_column`
- **THEN** the run stops with a compiler error
- **AND** the error reports how many negative values the source holds

#### Scenario: Missing key column
- **WHEN** the resolved bucket relation does not contain `bucket_key_column`
- **THEN** the run stops with a compiler error

### Requirement: UUID Key Range Partition

When the bucket key column is a non-null UUID column, every full-refresh bucket pass SHALL filter that column to one contiguous key range.

#### Scenario: UUID key
- **WHEN** the resolved bucket relation's `bucket_key_column` is a non-null UUID column
- **THEN** each bucket pass filters the key column with a lower and an upper UUID bound
- **AND** no bucket pass contains `reinterpretAsUInt64`

#### Scenario: Boundary key
- **WHEN** a key value equals an interior range boundary
- **THEN** the value matches exactly one bucket pass: the range above the boundary
- **AND** the range below the boundary does not match it

### Requirement: Key Statistics

A full refresh on a UUID key SHALL capture the row count, the key minimum, the key maximum and the snapshot maximum from one statistics statement.

#### Scenario: Captured bounds
- **WHEN** the statistics statement completes
- **THEN** the captured minimum and maximum equal the least and greatest `reinterpretAsUInt128` key values in the bucket relation

#### Scenario: 128-bit exactness
- **WHEN** keys sit near the top of the unsigned 128-bit range
- **THEN** the captured bounds keep full precision
- **AND** the published target contains every key

### Requirement: Balanced Boundary Coverage

Given row count `R`, `rows_per_bucket` `B`, bucket count `N = ceil(R / B)` and key bounds `m` and `M`, the materialization SHALL cover `[m, M]` with `N` ranges that neither gap nor overlap, where boundary `b_i = m + i * floor((M - m) / N) + min(i, (M - m) mod N)` and `b_N = M`.

#### Scenario: Remainder spread
- **WHEN** `M - m` is not divisible by `N`
- **THEN** the first `(M - m) mod N` ranges are one key value wider than the rest
- **AND** no range absorbs the whole remainder

#### Scenario: Zero step
- **WHEN** `M - m` is smaller than `N`, so `floor((M - m) / N)` is zero
- **THEN** boundaries advance by at most one key value
- **AND** distinct keys spread across buckets instead of collapsing into the last one

#### Scenario: Single bucket
- **WHEN** `R` is at most `B`
- **THEN** `N` is one and the single range covers `[m, M]` inclusive

### Requirement: Direct Key Comparison

The range predicate SHALL compare the key column directly against constant UUID literals, without a function applied to the column.

#### Scenario: Predicate shape
- **WHEN** a UUID bucket pass runs
- **THEN** both bounds appear as constant UUID literals in the statement
- **AND** the key column appears bare on the left of each comparison

### Requirement: Bucket Coverage

Every source row stamped at or below `S0` SHALL match exactly one UUID bucket predicate.

#### Scenario: Minimum and maximum keys
- **WHEN** a row carries the minimum or the maximum key
- **THEN** the row is included in the first or the last range respectively

#### Scenario: Clustered keys
- **WHEN** keys are clustered so ranges hold different row counts
- **THEN** every row is still covered by exactly one range
- **AND** bucket row counts may be uneven

#### Scenario: Writes above the bound
- **WHEN** a row is stamped above `S0`
- **THEN** no bucket pass reads it

### Requirement: One Bucket per Key

All versions of one key SHALL fall in the same bucket.

#### Scenario: Versions of one key
- **WHEN** one key has several versions with different snapshots
- **THEN** every version lands in the same bucket
- **AND** the target keeps one row for the key

### Requirement: Source Ordering Contract

A model whose `bucket_key_column` is a UUID SHALL point `bucket_ref` or `bucket_source` at a relation whose sorting key leads with that column.

#### Scenario: Ordered source
- **WHEN** the bucket relation is sorted by the key
- **THEN** each bucket pass reads only its range
- **AND** the rows read by all bucket passes together stay well below one source scan per bucket

#### Scenario: Unordered source
- **WHEN** the bucket relation is not sorted by the key
- **THEN** bucket contents stay correct
- **AND** each pass may scan the relation; the materialization does not enforce the ordering

### Requirement: Bucket Relation Contract

The materialization SHALL read counts and type information from the bucket relation resolved from `bucket_ref` or `bucket_source`, and that relation SHALL exist as a table at run time.

#### Scenario: Relation missing
- **WHEN** the resolved bucket relation does not exist
- **THEN** the run stops with a compiler error that names the model and the relation

#### Scenario: Relation is not a table
- **WHEN** the resolved bucket relation exists but is a view or another non-table relation
- **THEN** the run stops with a compiler error that names the relation type

#### Scenario: Relation missing the snapshot column
- **WHEN** the resolved bucket relation does not contain `bucket_snapshot_column`
- **THEN** the run stops with a compiler error

### Requirement: Error Reporting

Every configuration or relation compiler error SHALL carry the `bucketed_incremental:` prefix and SHALL name the offending config key and value, or the config key and the resolved relation (`<schema>.<identifier>`).

#### Scenario: Message content
- **WHEN** a configuration or relation check fails
- **THEN** the message names the offending config key and the value or relation it rejects
- **AND** the exact strings are listed in the [design Error reference](design.md#error-reference)

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
- **THEN** it costs one additional `max()` scan of the resolved bucket relation

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

#### Scenario: Relation sync
- **WHEN** a model sets `bucket_ref` or `bucket_source`
- **THEN** the model body SHALL read the same relation with the matching `{{ ref(...) }}` or `{{ source(...) }}` call
- **AND** the body's call registers the DAG edge; a call inside the materialization resolves the relation at run time but adds no dependency
- **AND** the materialization cannot inspect the model body, so a mismatch leaves uneven or empty buckets (see D2 in `design.md`)

### Requirement: Test Coverage

The suite SHALL cover, beyond the existing matrix:

- Both keys set and neither key set stop the run.
- `bucket_ref` set with 0, 3, non-list, and non-identifier elements stops the run; likewise for a string value.
- `bucket_source` set with 0, 1, 3, non-list, and non-identifier elements stops the run.
- `bucket_ref` with 1 element and with 2 elements each resolves and builds.
- `bucket_ref` and `bucket_source` accept a list or a tuple on both branches.
- The ref branch resolves through `ref()` and builds; the source branch resolves through `source()` and builds.
- The ref branch renders the count query and the detection query against the resolved relation (correct quoting and schema), not a hardcoded string.
- The relation contract (missing relation, non-table relation, missing key or snapshot column) holds for the ref branch, not only the source branch.
- Resolution failure (unknown model, undeclared source) stops the run with dbt's error.
- An incremental run does not resolve the bucket relation, but still rejects a wrong-shaped key.
- A UUID key build emits range predicates and no `reinterpretAsUInt64`.
- Interior UUID passes use `<` and the last pass uses `<=` on the upper bound.
- Every UUID pass carries the snapshot bound.
- Rows at the minimum, at an interior boundary and at the maximum are each in exactly one bucket.
- Versions of one key share a bucket and dedupe.
- Dense keys with a zero step spread across buckets.
- A remainder is spread across the first buckets.
- Bounds near the top of the 128-bit range keep full precision.
- An empty UUID source builds an empty table.
- Integer keys keep the modulo predicate.
- A UUID source sorted by the key reads far fewer rows than one source scan per bucket.

## Amendments

### 2026-10-02: UUID keys bucket by range

`bucket_key_column` UUID columns now bucket by contiguous key ranges derived from the source key minimum and maximum instead of `reinterpretAsUInt64(bucket_key_column)`. The requirements above state the current contract; integer keys are unchanged, there are no configuration changes, and published tables are unchanged.
