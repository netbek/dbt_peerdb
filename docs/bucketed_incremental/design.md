# bucketed_incremental Design

## Context

The warehouse mirrors operational tables into ClickHouse with PeerDB, then dedupes them in staging models by key. Some of those tables hold hundreds of millions of rows. A full refresh that runs as one `create table ... as select` can exhaust memory, monopolize the server for hours and fail late, after most of the work is done. Upstream `dbt-clickhouse` offers a single-CTAS full refresh and several incremental strategies; neither fits a source under continuous change.

ClickHouse gives each query a consistent snapshot but no snapshot that spans separate statements. A rebuild split into many statements therefore reads a different source state in each one. Without a guard, a row written after its bucket has been read never appears in the target, and no later incremental run selects it. `docs/plans/concurrent-writes-data-loss.md` records the analysis and the decision: pin every bucket to a captured snapshot bound, detect writes that land mid-build, and document the tie-safe watermark that lets the next incremental run recover them.

## Goals

1. Bound the work of a single statement by loading roughly `rows_per_bucket` rows per bucket.
2. Keep readers on a complete table: publish in one step; a failed build leaves the previous contents untouched.
3. Keep concurrent writes to append-only sources from being lost silently.
4. Keep incremental runs on the adapter's tested delete+insert path.
5. Stop with a clear error when the configuration cannot satisfy the design.

## Non-goals

- Resumable builds. A failed build restarts at bucket 0.
- Physical row deletes during a rebuild. A missed delete leaves a stale row; quiesce the source.
- Backdated writes. A row stamped below `S0` and landing after its bucket is not re-selected.
- Distributed tables and the `insert_overwrite`, `microbatch`, `append` and `legacy` strategies.
- Enforcing the model's watermark predicate. The materialization cannot inspect it.

## Terminology

| Term | Meaning |
|------|---------|
| Bucket | One pass over the source, filtered to a subset of keys |
| `bucket_key_column` | The key column that defines the buckets; must equal `unique_key` |
| `bucket_source_table` | The `database.table` relation the macro counts and type-checks |
| Snapshot column | A non-null `DateTime64(9)` column that orders writes, usually `_peerdb_synced_at` |
| `S0` | The maximum snapshot value captured before bucket 0 |
| `W` | The maximum snapshot value in the published target, at most `S0` |
| High watermark | The model's estimate of `W`, usually `max(_peerdb_synced_at)` over `{{ this }}` |
| Marker | The line `-- __BUCKET_PREDICATE__` that each bucket pass replaces |
| Full-refresh mode | `should_full_refresh()` or no existing relation or an existing view |

## Architecture

One materialization, two paths. The config validation and strategy checks run on both.

```
first run or --full-refresh ──► count ──► bucket loop ──► detect ──► publish
existing table, normal run   ──► delete+insert ──► grants, docs, hooks, commit
```

### Full-refresh path

1. **Resolve.** Load the existing relation, config values and relation handles: `target_relation` (`this` as a table), `intermediate_relation` (`<identifier>__dbt_tmp`) and `backup_relation` (`<identifier>__dbt_backup`).
2. **Validate.** Check every rule in [Configuration reference](#configuration-reference) and [Error reference](#error-reference). The checks read only config values, so they run before any database work.
3. **Prepare.** Drop leftover intermediate and backup relations, then run pre-hooks (outside and inside the transaction).
4. **Probe the source.** Resolve `bucket_source_table`, require a table, and read its columns with `adapter.get_columns_in_relation`. Infer the internal key type from the key dtype (`UUID`, signed `Int*`, unsigned `UInt*`; anything else stops the run) and check the snapshot dtype against `DateTime64(9)`.
5. **Count.** One query returns `count()` as `row_count`, `countIf(bucket_key_column < 0)` for integer keys, and `toString(max(bucket_snapshot_column))` as `snapshot_max`.
6. **Size.** `bucket_count = ceil(row_count / rows_per_bucket)`, at least 1 unless the source is empty. The macro logs row count, bucket count and `S0`.
7. **Bucket loop.** For bucket `i`: replace the marker with `where <key expression> % <bucket_count> = <i> and <snapshot column> <= <S0 literal>`; the first bucket creates the intermediate relation with a CTAS, the rest insert into it with `clickhouse__insert_into`. The key expression is `reinterpretAsUInt64(bucket_key_column)` for `uuid` and the bare column otherwise.
8. **Detect.** Query `select max(bucket_snapshot_column) > <S0 literal> as writes_detected from bucket_source_table`. `error` stops before the publish step; `warn` logs; `ignore` skips the query.
9. **Publish and finish.** Rename or exchange the intermediate relation into place, apply grants, persist docs, create indexes, run post-hooks, commit, drop the backup, run outside-transaction post-hooks.

An empty source skips the loop: the marker becomes `where 1 = 0`, and the CTAS builds an empty intermediate relation that publishes normally.

### Incremental path

1. **Validate.** The same config and strategy checks run first.
2. **Schema changes.** When `on_schema_change` is not `ignore`, call `adapter.check_incremental_schema_changes`, apply the changes with `clickhouse__apply_column_changes`, and reload the existing relation.
3. **Delete and insert.** Call the adapter macro `clickhouse__incremental_delete_insert(existing_relation, unique_key, incremental_predicates)`. It builds `<identifier>__dbt_new_data_<invocation_id>` from the model SQL, deletes matching keys from the target, inserts the temporary rows and drops the temporary table.
4. **Finish.** The same grants, docs, indexes, hooks and commit tail as the full-refresh path, without a publish step.

## Code layout

| Macro | Responsibility |
|-------|----------------|
| `macros/materializations/bucketed_incremental.sql` | The materialization: validation, both paths, publish |
| `macros/materializations/is_incremental.sql` | Upstream copy that also recognises `bucketed_incremental` |
| `clickhouse__incremental_delete_insert` (adapter) | Incremental delete+insert, including predicates |
| `clickhouse__apply_column_changes` (adapter) | `on_schema_change` DDL |
| `get_create_table_as_sql` (adapter) | First bucket and the empty-source build |
| `clickhouse__insert_into` (adapter) | Buckets after the first; honours enforced contracts |
| `exchange_tables_atomic` (adapter) | `EXCHANGE TABLES` publish step |
| `make_intermediate_relation`, `make_backup_relation` (adapter) | Relation names |

## Relation lifecycle

| Relation | Name | Created | Removed |
|----------|------|---------|---------|
| Target | `this` | Publish step of the first run | Never by this materialization |
| Intermediate | `<identifier>__dbt_tmp` | First bucket CTAS, or the empty build | Renamed to the target (first run) or to the backup name (rebuild) |
| Backup | `<identifier>__dbt_backup` | Publish step of a rebuild | After the commit |
| New data | `<identifier>__dbt_new_data_<invocation_id>` | Incremental path | End of the delete+insert macro |

Leftover intermediate and backup relations are dropped before the build. The publish step chooses between three shapes:

- No existing relation: rename the intermediate relation to the target.
- Existing relation with `can_exchange`: rename the intermediate relation to the backup name, then `EXCHANGE TABLES` backup and target. The exchange is atomic on database engines that support it; relation types and grants survive.
- Existing relation without `can_exchange` (views, engines without atomic exchange): rename the target to the backup name, then rename the intermediate relation to the target. Readers can see the target missing between the two renames.

A failure before the publish step leaves the target untouched: the macro writes to the target only during publish, and the next run clears the leftover intermediate relation. ClickHouse offers no transactional DDL, so the guarantee rests on the write order, not on rollback.

## Concurrency design

### No snapshot spans statements

A model that dedupes `_peerdb_synced_at` by key reads the source twice on an incremental run: once for the changed keys and once for all versions. Before ClickHouse 25.12 the two scans could see different states, and each bucket pass is always a separate statement. The `enable_shared_storage_snapshot_in_query` setting shares a snapshot only inside one query.

### Snapshot pinning

The count query captures `S0 = max(bucket_snapshot_column)`. Every bucket reads only rows with `snapshot_column <= S0`. A write landing mid-build carries a stamp at or above `S0` on a monotonic clock, so no bucket picks up a value written after the bound and no bucket sees a partial version of a row. The build is internally consistent at `S0`; a write stamped above `S0` is left for the next incremental run.

### Tie-safe watermark

The next incremental run re-selects a key when `snapshot >= W`, where `W` is the target's maximum snapshot and `W <= S0`:

- A write stamped `S_w > S0` is always recovered.
- A write stamped `S_w == S0` is recovered only by `>=`; with a strict `>` it can be missed forever. PeerDB's `_peerdb_synced_at` is `DateTime64(9) DEFAULT now64()`; `now64()` defaults to millisecond resolution, so ties are reachable.
- A write stamped `S_w < S0` (clock skew, backdated data) is not recovered: `W` can sit at or above `S_w`, and no later run re-selects the key until it is written again or the next full refresh.

The `>=` comparison is a model contract. The macro cannot inspect the model's predicate, so the README and the spec state the rule and the price: the batch at the watermark is re-read on every incremental run.

### Fail-closed detection

After the bucket loop, `select max(snapshot) > S0` compares the source with the bound again. `on_concurrent_writes` defaults to `error`: the run stops before the publish step and the target keeps its previous contents. `warn` logs and publishes, which converges only after a later incremental run; `ignore` skips the query and accepts silent divergence. The check costs one extra `max()` scan.

### Residual risk

| Case | Effect | Mitigation |
|------|--------|------------|
| Row stamped below `S0` landing after its bucket | Not re-selected while `W >= S_w`; stale until a later write to the key or the next full refresh | None in the macro |
| Physical row delete after its bucket | Stale row until the next full refresh; no version exists to re-select it | Quiesce the source for rebuilds |
| Clock skew across shards or replicas | Backdated stamps widen the first case | Single-node deployment or quiesced rebuilds |
| Write landing between the two incremental source scans on servers before 25.12 | A version can stay missed until the key changes again | Upgrade; the hazard is inherited from upstream |

## Configuration reference

| Config | Required | Default | Rules |
|--------|----------|---------|-------|
| `materialized` | Yes | – | `bucketed_incremental` |
| `bucket_key_column` | Yes | – | Bare identifier; a column of `bucket_source_table` |
| `bucket_snapshot_column` | Yes | – | Bare identifier; non-null `DateTime64(9)`; different from `bucket_key_column` |
| `bucket_source_table` | Yes | – | `database.table`, both bare identifiers; an existing table |
| `unique_key` | Yes | – | The single `bucket_key_column` |
| `rows_per_bucket` | No | `1000000` | Positive integer; booleans rejected |
| `on_concurrent_writes` | No | `error` | `warn`, `error` or `ignore` |
| `incremental_strategy` | No | adapter-resolved | Must resolve to `delete_insert` |
| `inserts_only` | No | `false` | Must be false |
| `on_schema_change` | No | `ignore` | `ignore`, `fail`, `append_new_columns`, `sync_all_columns` |
| `predicates`, `incremental_predicates` | No | `[]` | Passed to the delete+insert step |
| `partition_by` | No | – | Passed to `validate_incremental_strategy` |
| `engine`, `order_by`, `contract`, `grants`, `indexes`, `docs` | No | – | Standard DDL and lifecycle configs; unchanged by this materialization |

The profile needs `use_lw_deletes: true` and the server needs `allow_nondeterministic_mutations=1`. Without the profile opt-in the adapter resolves the default strategy to `legacy`, which the materialization rejects.

## Error reference

| Condition | Message |
|-----------|---------|
| `bucket_key_column` missing or not an identifier | `bucket_key_column is required and must be a bare column identifier (letters, digits, underscore)` |
| `bucket_snapshot_column` missing or not an identifier | `bucket_snapshot_column is required and must be a bare column identifier (letters, digits, underscore)` |
| Snapshot column equals bucket column | `bucket_snapshot_column must be a different column from bucket_key_column` |
| `bucket_source_table` missing or not `database.table` | `bucket_source_table is required and must have the form "database.table" with bare identifiers (letters, digits, underscore)` |
| `unique_key` does not equal `bucket_key_column` | `unique_key must be the single bucket_key_column "<column>", otherwise versions of one row split across buckets` |
| `rows_per_bucket` not a positive integer | `rows_per_bucket must be a positive integer` |
| `on_concurrent_writes` outside the enum | `on_concurrent_writes must be one of "warn", "error", "ignore"` |
| `inserts_only` true | `inserts_only is not supported; incremental runs always use delete+insert` |
| Resolved strategy is not `delete_insert` | `only the delete_insert incremental strategy is supported, got "<strategy>"` |
| Marker count is not one | `marker -- __BUCKET_PREDICATE__ must appear exactly once in the model SQL; found <n>` |
| `bucket_source_table` not found | `bucket_source_table "<table>" not found for model "<model>"` |
| `bucket_source_table` is not a table | `bucket_source_table "<table>" must be a table, got type "<type>"` |
| Key column missing from the source | `bucket_key_column "<column>" not found in bucket_source_table "<table>"` |
| Unsupported key column type | `bucket_key_column "<column>" has unsupported type "<dtype>"; expected a non-null UUID, signed integer or unsigned integer column` |
| Snapshot column missing from the source | `bucket_snapshot_column "<column>" not found in bucket_source_table "<table>"` |
| Snapshot dtype is not `DateTime64(9)` | `bucket_snapshot_column "<column>" has unsupported type "<dtype>"; it must be a non-null DateTime64(9) column` |
| Negative integer keys | `bucket_source_table "<table>" has <n> negative values in "<column>". Negative integer keys cannot be bucketed (the bucket maths uses modulo), so a full refresh would silently drop them` |
| No usable snapshot maximum | `bucket_snapshot_column "<column>" has no usable maximum in bucket_source_table "<table>"` |
| Concurrent write detected with `error` | `concurrent writes to bucket_source_table "<table>" detected during the rebuild (snapshot bound <S0> exceeded). Re-run against a quiesced source; the previous table was left untouched` |

Every message carries the `bucketed_incremental:` prefix.

## Design decisions

### D1: Inject the bucket predicate through a marker

**Decision.** The model marks the bucket filter location with a comment, and the macro replaces it per pass.

**Rationale.** The macro cannot infer where the filter belongs in arbitrary SQL. A marker keeps the model in control of its shape and keeps the predicate visible in the model.

**Consequences.** The full-refresh SQL must carry exactly one marker; validation turns a missing or duplicated marker into a clear error. The incremental branch has no marker because the marker lives in the `is_incremental() == false` branch.

### D2: Size buckets by counting the source

**Decision.** Run one count query against `bucket_source_table` and compute `ceil(row_count / rows_per_bucket)`.

**Rationale.** A fixed bucket count mis-sizes after growth: too few buckets keep statements large, too many add scans. Counting costs one cheap query compared with the build.

**Consequences.** The count table must stay in sync with the table the model reads, or buckets end up uneven or empty. Each bucket re-reads the source, so a rebuild costs about one source scan per bucket.

### D3: Validate strictly and early

**Decision.** Reject bad identifiers, mismatched key types, negative integer keys, unsupported strategies and unsupported snapshot types before any bucket runs.

**Rationale.** The failure modes are silent: a wrong key type buckets on the wrong value, a negative key is dropped by modulo, a non-`DateTime64(9)` snapshot breaks the recovery contract. An early error is cheaper than a corrupted table.

**Consequences.** Models with unusual but valid sources must adapt. The validation uses the source dtype strings exactly, so `Nullable(UUID)` and similar wrappers are rejected.

### D4: One strategy only

**Decision.** Require the adapter to resolve `delete_insert` and reject `inserts_only` and every other strategy.

**Rationale.** The bucket contract assumes one stable key per row, and the incremental recovery contract assumes a delete+insert watermark. `legacy`, `append` and `insert_overwrite` would break both in ways the macro cannot detect at run time.

**Consequences.** The profile must set `use_lw_deletes: true` and the server `allow_nondeterministic_mutations=1`; otherwise the adapter default resolves to `legacy` and the run stops with a message that says what to change.

### D5: Build into an intermediate relation and publish atomically

**Decision.** Buckets load into `<identifier>__dbt_tmp`, then rename or exchange into place.

**Rationale.** Readers must never see a half-built table, and a failed build must not destroy the working copy.

**Consequences.** A full refresh needs extra disk for two complete copies. On engines without atomic exchange the two renames expose a moment where the target is missing; `can_exchange` selects the atomic path when the engine supports it.

### D6: Pin the snapshot and fail closed

**Decision.** Capture `S0` before bucket 0, bound every bucket by `S0`, and compare `max(snapshot)` with `S0` after the loop. Default `on_concurrent_writes` to `error`.

**Rationale.** Separate statements cannot share a snapshot, so the macro cannot make the build read one state. It can make the build consistent at `S0` and refuse to publish when the source moved. Silently publishing a stale table is worse than a failed run.

**Consequences.** Rebuilds against live sources fail until the source quiets or the operator chooses `warn`. `warn` and `ignore` are explicit opt-outs. The check costs one `max()` scan.

### D7: Bucket uuid keys through `reinterpretAsUInt64`

**Decision.** Use `reinterpretAsUInt64(bucket_key_column) % N = i` for `uuid` keys and `bucket_key_column % N = i` for integer keys.

**Rationale.** ClickHouse has no modulo operator for `UUID`. Reinterpreting a UUID as an unsigned integer keeps the mapping stable and spreads keys.

**Consequences.** The mapping ignores the version and variant bits; distribution can be uneven for non-random UUIDs. Every version of a key still lands in one bucket.

### D8: No resumability

**Decision.** A failed build drops the work and restarts at bucket 0.

**Rationale.** Resumable state needs a durable progress marker and a way to reconcile partial buckets with writes that landed since. That machinery belongs in an orchestrator, not a dbt materialization.

**Consequences.** A failure near the end of a long rebuild repeats the earlier buckets. Operators size `rows_per_bucket` to keep a single bucket short.

### D9: Accept one source scan per bucket

**Decision.** Each bucket re-reads `bucket_source_table`.

**Rationale.** Materializing the source once defeats the purpose of a bounded-memory rebuild; a view or staging table merely moves the cost. Repeated bounded scans let ClickHouse prune and stream each pass.

**Consequences.** A rebuild reads the source about `N` times. Lowering `rows_per_bucket` lowers peak memory but raises total scan cost.

### D10: Infer the key type from the source column

**Decision.** The macro reads the key column dtype from `bucket_source_table` during the probe and sets an internal key type: `UUID` buckets by `reinterpretAsUInt64`, signed and unsigned integers bucket by value. Models declare only `bucket_key_column`.

**Rationale.** The probe already reads the source columns to confirm the key exists, so the dtype is available at no extra cost. Deriving behavior from it leaves one source of truth, with no way for a model to contradict the source it points at.

**Consequences.** Three key families need three bucket expressions, chosen in one place. An unsupported key type stops the run during the probe, before any bucket work. Supporting a new key family means extending the inference, not adding model-facing config.

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Upstream single-CTAS full refresh | No bound on memory or statement time; a failure discards all work |
| `row_number()` sharding | The window must sort the whole source in one statement, which is the memory problem again |
| One bucket per partition or per key range | Sources have no partition key, and ranges need a distribution map |
| `insert_overwrite` by partition | Needs `partition_by`, which sources lack, and replaces whole partitions |
| A materialized view for incremental maintenance | Moves complexity into DDL outside dbt's build graph and cannot backfill |
| External chunked backfill (for example Dagster-driven) | Splits the model's write path across two systems and two config surfaces |
| Read-time dedupe with `FINAL` | Leaves duplicate storage, slows queries and does not solve the rebuild memory problem |
| Snapshotting the source with `CREATE TABLE ... SNAPSHOT` | Not portable across engines and versions, and doubles storage before the build starts |

## Test strategy

The integration harness runs a real ClickHouse server in Docker with `allow_nondeterministic_mutations=1`, `log_queries` on, and the database engine that supports atomic exchange. A dbt project in `integration_tests/` includes this package by local path, seeds source tables, and exercises one parametrized model whose config values come from `--vars`.

| Layer | Scope | Examples |
|-------|-------|---------|
| Compile-level | Config validation, marker checks | Every error in [Error reference](#error-reference) |
| Integration | Bucket builds, dtypes, empty source, publish | Target contents; `EXCHANGE TABLES` in `system.query_log` |
| Contract | Snapshot bound, detection modes | `<=` bounds on every bucket query; mid-build writer test |
| Incremental | Delete+insert, schema change, watermark tie | Only touched keys replaced; a row stamped `S0` recaptured |

Tests assert observable effects: table contents, stdout run logs, and `system.query_log` entries after `SYSTEM FLUSH LOGS`. The concurrency test slows bucket zero with `sleepEachRow` and writes to the source from a background thread that waits for a bucket query to appear in `system.processes`, so the write always lands between the count query and the detection query.

## Open questions

- Whether to move the validation rules into the adapter so other materializations can reuse them.
- Whether a progress table under an orchestrator should own resumability.
- Whether to support sources that physically delete rows through a periodic full refresh instead of a quiesce.

## References

- `README.md` in this package: user-facing description and model example.
- `docs/plans/concurrent-writes-data-loss.md`: hazard analysis, worked example and decision record.
- Upstream materialization: `vendor/dbt-clickhouse/dbt/include/clickhouse/macros/materializations/incremental/incremental.sql` (adapter version 1.10.2).
- Adapter strategy resolution and validation: `vendor/dbt-clickhouse/dbt/adapters/clickhouse/impl.py`.
