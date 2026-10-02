# bucketed_incremental Design

> Status: v1 · last updated 2026-10-02

## Context

The warehouse mirrors operational tables into ClickHouse with PeerDB, then dedupes them in staging models by key. Some of those tables hold hundreds of millions of rows. A full refresh that runs as one `create table ... as select` can exhaust memory, monopolize the server for hours and fail late, after most of the work is done. Upstream `dbt-clickhouse` offers a single-CTAS full refresh and several incremental strategies; neither fits a source under continuous change.

ClickHouse gives each query a consistent snapshot but no snapshot that spans separate statements. A rebuild split into many statements therefore reads a different source state in each one. Without a guard, a row written after its bucket has been read never appears in the target, and no later incremental run selects it. `docs/plans/bucketed-incremental/concurrent-writes-data-loss.md` records the analysis and the decision: pin every bucket to a captured snapshot bound, detect writes that land mid-build, and document the tie-safe watermark that lets the next incremental run recover them.

UUID keys need one more consideration. The original modulo partition, `reinterpretAsUInt64(key) % N`, is not sargable, so every bucket scans the source. ClickHouse defines `using UUID = StrongTypedef<UInt128, UUIDTag>`, so UUID comparison is exactly `UInt128` comparison and a range predicate on the key is sargable against a table ordered by it. The internal `UInt128` swaps the canonical halves, which decides how each UUID version is distributed in ClickHouse key order; see [Modulo vs range by UUID flavor](#modulo-vs-range-by-uuid-flavor). When the key is a UUID, the model contract is that the bucket relation is `PRIMARY KEY id ORDER BY id`; the materialization does not verify the sorting key.

## Goals

1. Bound the work of a single statement by loading roughly `rows_per_bucket` rows per bucket.
2. Keep readers on a complete table: publish in one step; a failed build leaves the previous contents untouched.
3. Keep concurrent writes to append-only sources from being lost silently.
4. Keep incremental runs on the adapter's tested delete+insert path.
5. Stop with a clear error when the configuration cannot satisfy the design.
6. Let each UUID bucket pass read only its key range through the source's primary-key index.
7. Keep the bucket-sizing promise (`rows_per_bucket`) meaningful for evenly spread keys.

## Non-goals

- Resumable builds. A failed build restarts at bucket 0.
- Physical row deletes during a rebuild. A missed delete leaves a stale row; quiesce the source.
- Backdated writes. A row stamped below `S0` and landing after its bucket is not re-selected. Rare under PeerDB (shard skew, step-back, manual writes); routine for source-stamped own updated-at columns, which must quiesce for rebuilds.
- Distributed tables and the `insert_overwrite`, `microbatch`, `append` and `legacy` strategies.
- Enforcing the model's watermark predicate. The materialization cannot inspect it.
- Exactly equal row counts per bucket (quantiles, sampling).
- Enforcing or probing the source sorting key.
- A config flag choosing between range and modulo.
- Range bucketing for integer keys.

## Terminology

| Term | Meaning |
|------|---------|
| Bucket | One pass over the source, filtered to a subset of keys |
| `bucket_key_column` | The key column that defines the buckets; must equal `unique_key`. The repetition is intentional: it turns a silent bucketing corruption (versions of one row splitting across buckets) into a compile-time error (see D3) |
| `bucket_ref` / `bucket_source` | The two model configs that name the relation to count and type-check; set exactly one. `bucket_ref` lists one or two identifiers for `ref()`; `bucket_source` lists exactly two for `source()` |
| Bucket relation | The relation the set config resolves to through `ref()` or `source()`; the macro counts, probes and detects against it |
| `bucket_snapshot_column` | The snapshot column; names a column, not a threshold. A non-null `DateTime64(9)` column that orders writes, usually `_peerdb_synced_at`. Destination-stamped under PeerDB, source-stamped for own updated-at columns (see the provenance note in `concurrent-writes-data-loss.md`) |
| Snapshot column | The role `bucket_snapshot_column` plays: the column whose maximum orders the build |
| Key bounds | `m` and `M`: the least and greatest `reinterpretAsUInt128(bucket_key_column)` in the bucket relation, captured with the count |
| Stats statement | The single statement that captures the row count `R`, the key bounds and the snapshot maximum `S0` for a full refresh |
| Bucket count | `N = ceil(R / rows_per_bucket)` |
| Step | `floor((M - m) / N)`, the width shared by most ranges |
| Remainder | `rem = (M - m) - step * N`, the extra key values spread across the first ranges |
| Boundary | `b_i = m + i * step + min(i, rem)` for `i` in `0..N`, with `b_N = M` |
| Range | Bucket `i` covers `[b_i, b_{i+1})`, except the last bucket, which covers `[b_{N-1}, M]` |
| Boundary query | The table-free statement over `numbers(N)` that turns the bounds, step and remainder into UUID text |
| Range predicate | `key >= lower and key < upper` (last bucket: `key <= upper`), combined with the snapshot bound |
| `S0` | The snapshot bound (a value): the maximum snapshot value captured before bucket 0 |
| `W` | A watermark (a value): the maximum snapshot value in the published target, at most `S0` |
| High watermark | The model's estimate of `W`, usually `max(_peerdb_synced_at)` over `{{ this }}`. Watermarks are thresholds derived from the snapshot column; `S0`, `W`, and the high watermark are three different numbers with `high watermark <= W <= S0` across a build |
| Marker | The line `-- __BUCKET_PREDICATE__` that each bucket pass replaces |
| Full-refresh mode | `should_full_refresh()` or no existing relation or an existing view |

## Architecture

One materialization, two paths. The config validation and strategy checks run on both.

```
first run or --full-refresh ──► count ──► boundaries (uuid) ──► bucket loop ──► detect ──► publish
existing table, normal run   ──► delete+insert ──► grants, docs, hooks, commit
```

### Full-refresh path

1. **Resolve.** Load the existing relation, config values and relation handles: `target_relation` (`this` as a table), `intermediate_relation` (`<identifier>__dbt_tmp`) and `backup_relation` (`<identifier>__dbt_backup`).
2. **Validate.** Check every rule in the [Configuration reference](#configuration-reference). These checks read only config values, so they run before any database work; the probe-time errors in the [Error reference](#error-reference) surface at the steps below.
3. **Prepare.** Drop leftover intermediate and backup relations, then run pre-hooks (outside and inside the transaction).
4. **Probe the source.** Resolve the bucket relation from `bucket_ref` or `bucket_source` with `ref()`/`source()`, require a table, and read its columns with `adapter.get_columns_in_relation`. Infer the internal key type from the key dtype (`UUID`, signed `Int*`, unsigned `UInt*`; anything else stops the run) and check the snapshot dtype against `DateTime64(9)`.
5. **Count.** One query returns `count()` as `row_count`, `countIf(bucket_key_column < 0)` for integer keys, `toString(reinterpretAsUInt128(min(bucket_key_column)))` and `max(...)` for UUID keys, and `toString(max(bucket_snapshot_column))` as `snapshot_max`.
6. **Size.** `bucket_count = ceil(row_count / rows_per_bucket)`, at least 1 unless the source is empty. For UUID keys the macro also computes `step` and `remainder` in Jinja and runs one table-free boundary query:

   ```sql
   select
       number as bucket,
       toString(reinterpretAsUUID(
           toUInt128('<m>') + toUInt128(number) * toUInt128('<step>') + least(toUInt128(number), toUInt128('<rem>'))
       )) as lower_uuid,
       toString(reinterpretAsUUID(
           toUInt128('<m>') + toUInt128(number + 1) * toUInt128('<step>') + least(toUInt128(number + 1), toUInt128('<rem>'))
       )) as upper_uuid
   from numbers(<N>)
   order by bucket
   ```

   The statement returns `N` rows of canonical UUID text in bucket order. Bounds, step and remainder are spliced as quoted decimal strings because ClickHouse reads a bare decimal literal above `UInt64` as Float64 and rounds it. The macro logs row count, bucket count, `S0` and, for UUID keys, the key bounds and step.
7. **Bucket loop.** For bucket `i`: replace the marker with `where <key expression> % <bucket_count> = <i> and <snapshot column> <= <S0 literal>` for integer keys, or `where <key> >= toUUID('<lower>') and <key> < toUUID('<upper>') and <snapshot column> <= <S0 literal>` for UUID keys (the last bucket uses `<=` on the upper bound); the first bucket creates the intermediate relation with a CTAS, the rest insert into it with `clickhouse__insert_into`.
8. **Detect.** Query `select max(bucket_snapshot_column) > <S0 literal> as writes_detected from <bucket relation>`. `error` stops before the publish step; `warn` logs; `ignore` skips the query.
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

The count query captures `S0 = max(bucket_snapshot_column)`. Every bucket reads only rows with `snapshot_column <= S0`. A write landing mid-build carries a stamp at or above `S0` on a destination-monotonic clock (PeerDB `_peerdb_synced_at` on a single node), so no bucket picks up a value written after the bound and no bucket sees a partial version of a row. The build is internally consistent at `S0`; a write stamped above `S0` is left for the next incremental run. Source-stamped own updated-at columns must provide their own per-key monotonicity for this to hold.

### Tie-safe watermark

The next incremental run re-selects a key when `snapshot >= W`, where `W` is the target's maximum snapshot and `W <= S0`:

- A write stamped `S_w > S0` is always recovered.
- A write stamped `S_w == S0` is recovered only by `>=`; with a strict `>` it can be missed forever. PeerDB's `_peerdb_synced_at` is `DateTime64(9) DEFAULT now64()`; `now64()` defaults to millisecond resolution, so ties are reachable — the common PeerDB-native case.
- A write stamped `S_w < S0` (clock skew, backdated data) is not recovered: `W` can sit at or above `S_w`, and no later run re-selects the key until it is written again or the next full refresh. Under PeerDB this needs shard skew, a clock step backward, or manual writes; under own updated-at columns it is routine (late arrivals, backfills, source corrections).

The `>=` comparison is a model contract. The macro cannot inspect the model's predicate, so the README and the spec state the rule and the price: the batch at the watermark is re-read on every incremental run.

### Fail-closed detection

After the bucket loop, `select max(snapshot) > S0` compares the source with the bound again. `on_concurrent_writes` defaults to `error`: the run stops before the publish step and the target keeps its previous contents. `warn` logs and publishes, which converges only after a later incremental run; `ignore` skips the query and accepts silent divergence. The check costs one extra `max()` scan. It compares maxima only: it catches writes that raise `max` above `S0`, not truncates, TTL expiry, or physical deletes that lower the source without raising `max` (see Residual risk).

### Residual risk

| Case | Effect | Mitigation |
|------|--------|------------|
| Row stamped below `S0` landing after its bucket — PeerDB: shard skew, step-back, manual writes | Not re-selected while `W >= S_w`; stale until a later write to the key or the next full refresh | None in the macro |
| Row stamped below `S0` landing after its bucket — own updated-at column: late, backfilled, or corrected source data | Same effect | Quiesce the source for rebuilds and follow with an incremental run (required, not optional) |
| Physical row delete after its bucket | Stale row until the next full refresh; no version exists to re-select it | Quiesce the source for rebuilds |
| Clock skew across shards or replicas | Backdated stamps widen the first case | Single-node deployment or quiesced rebuilds |
| Write landing between the two incremental source scans on servers before 25.12 | A version can stay missed until the key changes again | Upgrade; the hazard is inherited from upstream |

### UUID range edge cases

| Case | Behavior |
|------|----------|
| Uniform random UUIDs | Range widths map to roughly equal row counts |
| Clustered keys | Ranges stay contiguous and complete; some buckets exceed `rows_per_bucket` (D17) |
| `step = 0`, distinct dense keys | Balanced widths of 0 or 1 keep at most one key per bucket |
| `step = 0`, duplicate keys | All rows of one key share a bucket, as with modulo |
| Single row or `R <= B` | One range `[m, M]` inclusive |
| Empty source | Empty-build path; no boundary query |
| Keys at zero or all-ones UUID | Ordinary bounds; no overflow because `b_N = M < 2^128` |
| Bounds near 2^128 | Quoted decimal literals parse exactly; a bare literal is read as Float64 and rounded |
| Rows stamped above `S0` | Excluded from every range by the snapshot bound |
| Bounds inflated by rows above `S0` | No coverage impact; ranges still contain every row at or below `S0` |
| Unordered source | Correct contents, no pruning |

### Modulo vs range by UUID flavor

ClickHouse stores a UUID as a `UInt128` with the canonical 64-bit halves swapped, so `ORDER BY key` sorts by the canonical low half first and the canonical high half second. The modulo operand, `reinterpretAsUInt64(key)`, copies the low 64 bits of that value, which is the canonical high half. Range splits the ClickHouse key order; modulo hashes the canonical high half. The swap inverts the usual intuition about UUID versions: v7 keeps its timestamp in the canonical high half, so a v7 table is ordered by the random `rand_b` in the low half; v1 and v6 keep the node and clock sequence in the low half, so their order is node blocks, then time.

`R` is the row count, `B` is `rows_per_bucket` and `N = ceil(R / B)`. The table assumes a source sorted by the key, which the model contract requires.

| UUID flavor | ClickHouse key order | Modulo rows read | Range rows read | Modulo peak rows/statement | Range peak rows/statement | Modulo balance | Range balance |
|-------------|----------------------|------------------|-----------------|----------------------------|---------------------------|----------------|---------------|
| v4, v5, v3 | random | `N * R` | `R` | `B` | `B` | even | even |
| v7, random `rand_b` | random | `N * R` | `R` | `B` | `B` | even | even |
| v7, monotonic `rand_b`, reseeded per millisecond | generation order within a millisecond, random across milliseconds | `N * R` | `R` | `B` | `B` | even | even |
| v1, v6, single node | node and clock-sequence block, then time | `N * R` | `R` | up to `R`: a regular cadence leaves every row in one residue | up to `R`: a burst window lands in one range | unreliable | arrival-rate dependent |
| Dense counter keys | counter order | `N * R` | `R` | `B` | up to `R`: a counter band is one burst | even | burst-dependent |
| Clustered or low-entropy keys | clustered | `N * R` | `R` | `B` when the hashed bits vary, otherwise up to `R` | up to `R`: the hot range | flavor-dependent | skewed |

An unsorted source removes the range advantage: both predicates read `N * R` rows and keep the same per-statement peaks, because range prunes nothing.

Other differences:

| Dimension | Modulo | Range |
|-----------|--------|-------|
| Sargable | never | when the sorting key leads with the key |
| Cost per scanned row | `reinterpretAsUInt64` and `%` | two constant comparisons |
| Extra statements | none | key bounds in the stats query and one table-free boundary query |
| Key bits used | canonical high 64 only | the full 128-bit key order |
| `rows_per_bucket` promise | exact when the hashed bits are random | only for evenly spread keys |
| Worst memory case | time-based UUID with a regular cadence | clustered keys with a burst |

The modulo fallback has no niche worth a code path. Random-key flavors stay balanced under range and read `R` rows instead of `N * R`; time-ordered flavors are where modulo is most likely to be degenerate; dense counter keys belong in an integer column, which already uses modulo.

## Configuration reference

| Config | Required | Default | Rules |
|--------|----------|---------|-------|
| `materialized` | Yes | – | `bucketed_incremental` |
| `bucket_key_column` | Yes | – | Bare identifier; a column of the bucket relation |
| `bucket_snapshot_column` | Yes | – | Bare identifier; non-null `DateTime64(9)`; different from `bucket_key_column` |
| `bucket_ref` | One of the pair | – | List or tuple of one or two bare identifiers, matching `ref()`; resolves the bucket relation |
| `bucket_source` | One of the pair | – | List or tuple of exactly two bare identifiers, matching `source()`; resolves the bucket relation |
| `unique_key` | Yes | – | The single `bucket_key_column` |
| `rows_per_bucket` | No | `100000` | Positive integer, minimum 1; booleans rejected; see D9 for scan tradeoff |
| `on_concurrent_writes` | No | `error` | `warn`, `error` or `ignore` |
| `incremental_strategy` | No | adapter-resolved | Must resolve to `delete_insert` |
| `inserts_only` | No | `false` | Must be false |
| `on_schema_change` | No | `ignore` | `ignore`, `fail`, `append_new_columns`, `sync_all_columns` |
| `predicates`, `incremental_predicates` | No | `[]` | Passed to the delete+insert step |
| `partition_by` | No | – | Passed to `validate_incremental_strategy`; the adapter's create table also emits it as `PARTITION BY`. Keep cardinality bounded (100–1,000 partitions max): partition by a low-cardinality column such as `region`, never by the key |
| `engine`, `order_by`, `contract`, `grants`, `indexes`, `docs` | No | – | Standard DDL and lifecycle configs; unchanged by this materialization |

The profile needs `use_lw_deletes: true`. Without the opt-in the adapter resolves the default strategy to `legacy`, which the materialization rejects. The adapter must also be able to enable `allow_nondeterministic_mutations` for its session; a user constrained against `SET` needs the setting in its profile. ClickHouse enforces the setting only on Replicated engines, where it rejects the delete's `IN (SELECT ...)` predicate.

## Error reference

| Condition | Message |
|-----------|---------|
| `bucket_key_column` missing or not an identifier | `bucket_key_column is required and must be a bare column identifier (letters, digits, underscore), got "<value>"` |
| `bucket_snapshot_column` missing or not an identifier | `bucket_snapshot_column is required and must be a bare column identifier (letters, digits, underscore), got "<value>"` |
| Snapshot column equals bucket column | `bucket_snapshot_column must be a different column from bucket_key_column` |
| Neither bucket relation key set | `set exactly one of bucket_ref or bucket_source, but neither was set` |
| Both bucket relation keys set | `set exactly one of bucket_ref or bucket_source, but both were set (bucket_ref="<value>", bucket_source="<value>")` |
| `bucket_ref` wrong shape | `bucket_ref must be a list or tuple of one or two bare identifiers (letters, digits, underscore) matching ref() arguments, got "<value>"` |
| `bucket_source` wrong shape | `bucket_source must be a list or tuple of exactly two bare identifiers (letters, digits, underscore) matching source() arguments, got "<value>"` |
| `unique_key` does not equal `bucket_key_column` | `unique_key must be the single bucket_key_column "<column>", otherwise versions of one row split across buckets` |
| `rows_per_bucket` not a positive integer | `rows_per_bucket must be a positive integer (>= 1)` |
| `on_concurrent_writes` outside the enum | `on_concurrent_writes must be one of "warn", "error", "ignore", got "<value>"` |
| `inserts_only` true | `inserts_only is not supported; incremental runs always use delete+insert` |
| Resolved strategy is not `delete_insert` | `only the delete_insert incremental strategy is supported, got "<strategy>". Set incremental_strategy="delete_insert"; it requires use_lw_deletes: true in the profile and a dbt user allowed to set allow_nondeterministic_mutations` |
| Marker count is not one | `marker -- __BUCKET_PREDICATE__ must appear exactly once in the model SQL; found <n>` |
| Bucket relation not found | `<key> "<schema>.<identifier>" not found for model "<model>"` |
| Bucket relation is not a table | `<key> "<schema>.<identifier>" must be a table, got type "<type>"` |
| Key column missing from the relation | `bucket_key_column "<column>" not found in <key> "<schema>.<identifier>"` |
| Unsupported key column type | `bucket_key_column "<column>" has unsupported type "<dtype>"; expected a non-null UUID, signed integer or unsigned integer column` |
| Snapshot column missing from the relation | `bucket_snapshot_column "<column>" not found in <key> "<schema>.<identifier>"` |
| Snapshot dtype is not `DateTime64(9)` | `bucket_snapshot_column "<column>" has unsupported type "<dtype>"; it must be a non-null DateTime64(9) column` |
| Negative integer keys | `<key> "<schema>.<identifier>" has <n> negative values in "<column>". Negative integer keys cannot be bucketed (the bucket maths uses modulo), so a full refresh would silently drop them` |
| No usable snapshot maximum (defensive; unreachable for a non-null `DateTime64(9)` column) | `bucket_snapshot_column "<column>" has no usable maximum in <key> "<schema>.<identifier>"` |
| Concurrent write detected with `error` | `concurrent writes to <key> "<schema>.<identifier>" detected during the rebuild (snapshot bound <S0> exceeded). Re-run against a quiesced source; the previous table was left untouched` |
| Concurrent write detected with `warn` (log) | `WARNING: concurrent writes to <key> "<schema>.<identifier>" detected during the rebuild; run an incremental afterwards to converge` |

Every message carries the `bucketed_incremental:` prefix and ends with a
period; the table omits the trailing period. In the table, `<key>` is
`bucket_ref` or `bucket_source`, and `<schema>.<identifier>` is the resolved
bucket relation (ClickHouse keeps `database` empty). Resolution failures
(unknown model, unknown package, undeclared source) surface as dbt's own
error and are not listed here.

## Design decisions

### D1: Inject the bucket predicate through a marker

**Decision.** The model marks the bucket filter location with a comment, and the macro replaces it per pass.

**Rationale.** The macro cannot infer where the filter belongs in arbitrary SQL. A marker keeps the model in control of its shape and keeps the predicate visible in the model.

**Consequences.** The full-refresh SQL must carry exactly one marker; validation turns a missing or duplicated marker into a clear error. The incremental branch has no marker because the marker lives in the `is_incremental() == false` branch. Replacement is a verbatim string splice, not SQL-aware: the marker stands where a `WHERE` clause belongs, so models must not put a `WHERE` before it and must append further full-refresh conditions with `AND` after it.

### D2: Size buckets by counting the source

**Decision.** Run one count query against the bucket relation and compute `ceil(row_count / rows_per_bucket)`.

**Rationale.** A fixed bucket count mis-sizes after growth: too few buckets keep statements large, too many add scans. Counting costs one cheap query compared with the build.

**Consequences.** The bucket relation must stay in sync with the relation the model reads, or buckets end up uneven or empty. Each bucket re-reads the relation, so a rebuild costs about one source scan per bucket.

### D3: Validate strictly and early

**Decision.** Reject bad identifiers, mismatched key types, negative integer keys, unsupported strategies and unsupported snapshot types before any bucket runs.

**Rationale.** The failure modes are silent: a wrong key type buckets on the wrong value, a negative key is dropped by modulo, a non-`DateTime64(9)` snapshot breaks the recovery contract. An early error is cheaper than a corrupted table.

**Consequences.** Models with unusual but valid sources must adapt. The validation uses the source dtype strings exactly, so `Nullable(UUID)` and similar wrappers are rejected.

### D4: One strategy only

**Decision.** Require the adapter to resolve `delete_insert` and reject `inserts_only` and every other strategy.

**Rationale.** The bucket contract assumes one stable key per row, and the incremental recovery contract assumes a delete+insert watermark. `legacy`, `append` and `insert_overwrite` would break both in ways the macro cannot detect at run time.

**Consequences.** The profile must set `use_lw_deletes: true`, and the adapter must be able to enable `allow_nondeterministic_mutations` for its session; otherwise the adapter default resolves to `legacy` and the run stops with a message that says what to change.

### D5: Build into an intermediate relation and publish atomically

**Decision.** Buckets load into `<identifier>__dbt_tmp`, then rename or exchange into place.

**Rationale.** Readers must never see a half-built table, and a failed build must not destroy the working copy.

**Consequences.** A full refresh needs extra disk for two complete copies. On engines without atomic exchange the two renames expose a moment where the target is missing; `can_exchange` selects the atomic path when the engine supports it.

### D6: Pin the snapshot and fail closed

**Decision.** Capture `S0` before bucket 0, bound every bucket by `S0`, and compare `max(snapshot)` with `S0` after the loop. Default `on_concurrent_writes` to `error`.

**Rationale.** Separate statements cannot share a snapshot, so the macro cannot make the build read one state. It can make the build consistent at `S0` and refuse to publish when the source moved. Silently publishing a stale table is worse than a failed run.

**Consequences.** Rebuilds against live sources fail until the source quiets or the operator chooses `warn`. `warn` and `ignore` are explicit opt-outs. The check costs one `max()` scan.

### D7: Bucket uuid keys by contiguous range

**Decision.** Use contiguous UUID ranges derived from the key minimum and maximum for `uuid` keys and `bucket_key_column % N = i` for integer keys.

**Rationale.** ClickHouse has no modulo operator for `UUID`, and `reinterpretAsUInt64(...) % N` is not sargable, so every bucket scans the source. A UUID is a strong typedef of `UInt128`, so reinterpretation preserves order and a range predicate uses the source's primary-key index when the table is ordered by the key.

**Consequences.** Bucket row counts depend on the key distribution: clustered keys can produce buckets larger than `rows_per_bucket` (see D17 and [Modulo vs range by UUID flavor](#modulo-vs-range-by-uuid-flavor)). Existing UUID models keep the same output but emit different statements.

### D8: No resumability

**Decision.** A failed build drops the work and restarts at bucket 0.

**Rationale.** Resumable state needs a durable progress marker and a way to reconcile partial buckets with writes that landed since. That machinery belongs in an orchestrator, not a dbt materialization.

**Consequences.** A failure near the end of a long rebuild repeats the earlier buckets. Operators size `rows_per_bucket` to keep a single bucket short.

### D9: Accept one source scan per bucket

**Decision.** Each bucket re-reads the bucket relation.

**Rationale.** Materializing the source once defeats the purpose of a bounded-memory rebuild; a view or staging table merely moves the cost. Repeated bounded scans let ClickHouse prune and stream each pass.

**Consequences.** A rebuild reads the relation about `N` times. The integer modulo predicate is non-sargable against `ORDER BY`, so each integer-keyed pass is effectively a full scan (the snapshot bound prunes only when the source is ordered or partitioned by snapshot). UUID range passes prune when the source is ordered by the key. Lowering `rows_per_bucket` lowers peak memory but raises total scan cost.

### D10: Infer the key type from the source column

**Decision.** The macro reads the key column dtype from the bucket relation during the probe and sets an internal key type: `UUID` buckets by contiguous range, signed and unsigned integers bucket by modulo. Models declare only `bucket_key_column`.

**Rationale.** The probe already reads the source columns to confirm the key exists, so the dtype is available at no extra cost. Deriving behavior from it leaves one source of truth, with no way for a model to contradict the source it points at.

**Consequences.** Two key families need two bucket predicates (range for UUID, modulo for integers), chosen in one place. An unsupported key type stops the run during the probe, before any bucket work. Supporting a new key family means extending the inference, not adding model-facing config.

### D11: Resolve the bucket relation through `ref()` / `source()`

**Decision.** Replace the `database.table` string with `bucket_ref` and `bucket_source`, exactly one per model, resolved through dbt's `ref()`/`source()`. The macro counts, probes and detects against the returned Relation object.

**Rationale.** dbt resolves the schema, quoting and package identity, so the macro never splits a string or assumes schema equals database, and it sees the same manifest object the model body uses. Two keys are needed because a two-element `ref(package, model)` is indistinguishable from `source(source, table)` by length alone.

**Consequences.** Config shape is validated before any database work; resolution failures surface as dbt's own error. A `ref()`/`source()` call inside the materialization adds no DAG edge, so the model body must contain the matching call or the bucket relation may build late. Versioned refs stay out of reach: `ref()` takes `version` as a keyword argument and the list has no slot for it.

### D12: Spread the boundary remainder across the first ranges

**Decision.** Use `b_i = m + i*step + min(i, rem)` instead of the simpler `b_i = m + i*step` with the last range extended to `M`.

**Rationale.** Plain floor division leaves the whole remainder in the last range and collapses to a single range when `step` is zero. The balanced form gives every range a width of `step` or `step + 1`; for dense, distinct keys with `step = 0` it still produces at most one key per bucket. The extra cost is one `least()` term per boundary.

**Consequences.** Boundaries differ from the originally proposed query; tests assert balanced widths. For the target case (random UUIDs) both forms are equivalent because `rem << step`.

### D13: Wrap the aggregate and return text

**Decision.** The stats statement uses `toString(reinterpretAsUInt128(min(bucket_key_column)))` and `toString(reinterpretAsUInt128(max(bucket_key_column)))`.

**Rationale.** Wrapping the aggregate computes min/max on the bare UUID column, avoiding a per-row cast and keeping any part-level min/max metadata applicable. Fetching text sidesteps agate type inference for 128-bit values, matching the existing `toString(max(bucket_snapshot_column))` pattern.

**Consequences.** The stats statement is no longer metadata-only: `count()` alone can be answered from table metadata, while the bounds read the key column (or its min/max projection when ClickHouse has one). That is one column scan against `N` saved bucket scans.

### D14: Compute boundaries once, splice quoted literals

**Decision.** Compute `step` and `remainder` in Jinja from the decimal bounds, run the boundary query once, and splice `toUUID('<text>')` literals into each bucket predicate.

**Rationale.** Python integers are exact, so no 128-bit float division. ClickHouse owns the `UInt128` to UUID text mapping (`reinterpretAsUUID`), so the macro never encodes the internal byte layout. Constant literals keep predicates readable, testable and sargable, and the boundary statement is table-free.

**Consequences.** One extra trivial statement per UUID full refresh. Bounds, step and remainder are spliced as quoted decimal strings (`toUInt128('<value>')`): ClickHouse reads a bare decimal literal above `UInt64` as Float64 and rounds it, while string parsing is exact.

### D15: Make the last range inclusive

**Decision.** Interior predicates use `key < upper`; the last predicate uses `key <= upper`.

**Rationale.** `b_N` equals the maximum key `M`, so a strict `<` would drop the maximum key. Only the last range needs the inclusive form.

**Consequences.** The boundary query returns the same shape for every bucket; the loop special-cases only the comparison operator.

### D16: Choose the strategy from the key dtype, not config

**Decision.** UUID columns always use range bucketing; no flag selects the strategy.

**Rationale.** The probe already infers the key type from the source, so the source dtype stays the single source of truth. Range wins on I/O for the contract's sorted source; the cases where modulo bounds per-statement memory better are unsorted or clustered sources that the contract excludes, or counter keys that belong in an integer column (see [Modulo vs range by UUID flavor](#modulo-vs-range-by-uuid-flavor)). A flag would add surface and a way to misconfigure.

**Consequences.** Existing UUID models change statement shape with no opt-out; output is unchanged. Integer keys are untouched.

### D17: Document ordering and skew; do not detect

**Decision.** The design records the source-ordering contract and the skew caveat; the macro neither probes the sorting key nor measures per-range row counts.

**Rationale.** Probing the sorting key adds a query and changes no behavior; detecting skew needs another pass over the source, which is the cost this feature removes. Random UUIDs spread uniformly, and the model contract guarantees the ordering.

**Consequences.** A clustered key distribution (v1/v6 and counter keys; v7 keys are random in ClickHouse order) can produce buckets larger than `rows_per_bucket`; this is the price of index locality and is stated in the spec and README.

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Upstream single-CTAS full refresh | No bound on memory or statement time; a failure discards all work |
| `row_number()` sharding | The window must sort the whole source in one statement, which is the memory problem again |
| One bucket per partition | Sources have no partition key |
| Keep modulo for UUID | Not sargable (`N` full scans per rebuild) and unreliable for time-based UUIDs under a regular cadence; see [Modulo vs range by UUID flavor](#modulo-vs-range-by-uuid-flavor) |
| Plain floor-division boundaries | Last range absorbs the remainder; dense keys collapse into one range |
| `quantilesExact` boundaries | Reads and sorts every key in one statement, the memory problem the materialization exists to avoid |
| Approximate quantiles or sampling | Nondeterministic boundaries and no exact coverage guarantee |
| Config flag for the strategy | Unnecessary surface; the source dtype is the single source of truth |
| Format UUID text in Jinja | Encodes the internal `UInt128`/UUID mapping in the macro |
| Probe `system.tables.sorting_key` | Adds a query and cannot change behavior |
| Range bucketing for integers too | Out of scope; negative keys and type widths need their own design |
| `insert_overwrite` by partition | Needs `partition_by`, which sources lack, and replaces whole partitions |
| A materialized view for incremental maintenance | Moves complexity into DDL outside dbt's build graph and cannot backfill |
| External chunked backfill (for example Dagster-driven) | Splits the model's write path across two systems and two config surfaces |
| Read-time dedupe with `FINAL` | Leaves duplicate storage, slows queries and does not solve the rebuild memory problem |
| Snapshotting the source with `CREATE TABLE ... SNAPSHOT` | Not portable across engines and versions, and doubles storage before the build starts |

## Test strategy

The integration harness runs ClickHouse 26.3.33.24 as a local server managed by `clickhousectl` (HTTP `18123`, TCP `19000`) on the default Atomic database engine, which supports `EXCHANGE TABLES`; `scripts/install-clickhouse.sh` enables query logging. The pytest suite in `tests/` drives a fixture dbt project at `tests/fixtures/dbt` that includes this package by local path. Each config variation has its own model file: distinct `--vars` values invalidate dbt's partial-parse cache, so fixed per-model configs are cheaper than one parametrized model. Data-only scenarios reuse a model and reshape the source.

| Layer | Scope | Examples |
|-------|-------|---------|
| Compile-level | Config validation, marker checks | Every error in [Error reference](#error-reference), including `bucket_ref`/`bucket_source` exclusivity and shape |
| Integration | Bucket builds, dtypes, empty source, publish | Target contents; `EXCHANGE TABLES` in `system.query_log` |
| Contract | Snapshot bound, detection modes | `<=` bounds on every bucket query; mid-build writer test |
| UUID range | Predicate shapes, boundary maths, precision | `>=`/`<` interior and `<=` last; balanced remainder; zero step; bounds near `2^128` |
| Pruning | Primary-key locality | UUID source `order by id` with several granules; per-statement `read_rows` stays far below `N` source scans |
| Incremental | Delete+insert, schema change, watermark tie | Only touched keys replaced; a row stamped `S0` recaptured |

`tests/helpers.py` captures both observable effects per run: the dbt events collected via `capture_events=True` and the statements in `system.query_log` since a server timestamp marker, read after `SYSTEM FLUSH LOGS` and filtered of the harness's own reads and the adapter's atomic-exchange probe. It also provides `create_source(order_by=...)`, `insert_generated_uuid_rows`, `clickhouse_uuid`/`clickhouse_uuid_value` (ClickHouse swaps the UUID's 64-bit halves) and per-statement `read_rows` via `ModelRun.insert_statements()`. The concurrency tests slow the build with `sleepEachRow` (pinned to one thread), and `LateWriter` runs a background thread that polls `system.processes` for that query, then inserts a row stamped with `now64(9)`, so the write always lands between the count query and the detection query.

## Open questions

- Whether to move the validation rules into the adapter so other materializations can reuse them.
- Whether a progress table under an orchestrator should own resumability.
- Whether to support sources that physically delete rows through a periodic full refresh instead of a quiesce.
- Whether a future revision should measure per-range row counts and warn on heavy skew; today the extra scan is not worth it.
- Whether integer keys with a leading sort key should get the same range treatment.

## References

- `README.md` in this package: user-facing description and model example.
- `docs/plans/bucketed-incremental/concurrent-writes-data-loss.md`: hazard analysis, worked example and decision record.
- `docs/plans/bucketed-incremental/integration-test-plan.md`: suite scope, fixture layout and test matrix.
- `docs/plans/bucketed-incremental/integration-test-implementation.md`: implemented harness, findings and coverage.
- Upstream materialization: `vendor/dbt-clickhouse/dbt/include/clickhouse/macros/materializations/incremental/incremental.sql` (adapter version 1.10.3).
- Adapter strategy resolution and validation: `vendor/dbt-clickhouse/dbt/adapters/clickhouse/impl.py`.
- ClickHouse `base/base/UUID.h`: `using UUID = StrongTypedef<UInt128, UUIDTag>`.
- ClickHouse `src/Functions/reinterpretAs.cpp`: `reinterpretAsUInt128` accepts UUID; `reinterpretAsUUID` returns UUID; `reinterpretAsUInt64` copies the low half of the internal `UInt128`.
- `tests/helpers.py`: `clickhouse_uuid` and `clickhouse_uuid_value` encode the UUID halves swap.
- ClickHouse `src/Storages/MergeTree/MergeTreeData.cpp`: implicit min/max count projection for primary-key columns.
