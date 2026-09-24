# bucketed_incremental integration tests: findings and implementation record

## Findings

### F1: `Nullable(...)` key and snapshot columns were accepted (fixed)

`dbt.adapters.clickhouse.column.ClickHouseColumn` strips `Nullable(...)` and
`LowCardinality(...)` from `dtype` and exposes them as `is_nullable` and
`is_low_cardinality`; `dtype` for `Nullable(UUID)` is `UUID`. The materialization's
probe read `col.dtype`, so it accepted wrapped keys and nullable `DateTime64(9)`
snapshots and failed later with a confusing ClickHouse error (`Sorting key
contains nullable columns`, code 44). This contradicted the spec ("nullable,
wrapped, or of any other type" must stop the run).

Fix: the probe reads `col.data_type` for the error messages and rejects
`col.is_nullable`/`col.is_low_cardinality` explicitly, so wrapped columns stop the
run regardless of adapter `data_type` behaviour
(`macros/materializations/bucketed_incremental.sql`). Coverage:
`test_key_column_unsupported_type` includes `Nullable(UUID)`,
`LowCardinality(UUID)` and `LowCardinality(Nullable(UUID))`;
`test_snapshot_column_unsupported_type` includes `Nullable(DateTime64(9))`
(ClickHouse rejects `LowCardinality(DateTime64(9))`). The adapter assumption is
pinned by `test_clickhouse_column_wrapper_flags` in `tests/test_clickhouse.py`.

### F2: package `is_incremental()` must be called package-qualified

dbt resolves plain macro calls (`is_incremental()`) only from the root project,
then the adapter/global namespaces; macros from installed packages are reachable
only by package-qualified name (`dbt_peerdb.is_incremental()`). Proven in the
fixture: on a second run `adapter.get_relation` returned the target table, yet
`is_incremental()` was `False` while `dbt_peerdb.is_incremental()` was `True`.

Consequence: both the adapter's and the global macro only recognise
`incremental`/`distributed_incremental`, so models written with the plain
`{% if is_incremental() %}` shape compile the **full-history branch** on every
run. Data stays correct because the materialization still takes the
delete+insert path, but every incremental run rebuilds all keys instead of the
changed ones, and the `>=` watermark contract is bypassed.

Decision: models call the package macro as `dbt_peerdb.is_incremental()`. The
fixture's `macros/bi_model.sql` uses the qualified call, and the root-project
delegation (`tests/fixtures/dbt/macros/is_incremental.sql`) was deleted because
no plain calls remain. `README.md` documents the qualified call.

### F3: `system.query_log` assertions restored (resolved)

The local server originally had no `<query_log>` config section:

```
SystemLog: Not creating system.query_log since corresponding section 'query_log' is missing from config
```

The ClickHouse config now enables the table (`scripts/install-clickhouse.sh`,
`~/.clickhouse/configs/dbt_peerdb.yaml`), so the harness reads executed statements
from `system.query_log` after `SYSTEM FLUSH LOGS`. The default
`flush_interval_milliseconds` of 7500 ms is left untouched: the explicit flush is
synchronous and keeps the tests deterministic. The server text log parser was
removed. Capture noise is filtered out in Python: the test's own `system.query_log`
reads and `SYSTEM FLUSH LOGS`, plus the adapter's atomic-exchange capability probe
(`__dbt_exchange_test_*`).

Statements are selected with `is_initial_query = 1` and a terminal `type`
(`QueryFinish`, `ExceptionWhileProcessing`, `ExceptionBeforeStart`), so a statement
contributes one row. The run timestamp marker is the server's
`toUnixTimestamp64Micro(now64(6))`, compared against `event_time_microseconds`,
which avoids timezone-rendered timestamps entirely.

### F4: lightweight delete predicate semantics

`predicates` are applied only to the delete, not to the insert. A changed key that
does not satisfy the predicate is left in the target and the new row is inserted
alongside it (duplicate), which is expected adapter behaviour. The test fixture
uses `predicates=['id >= 0']` so the delete applies to every changed key; a
predicate that excludes a changed key would leave a duplicate by design.

### F5: `is_incremental()` was masking other tests

Before F2 was understood, the `>=` tie-safe watermark test passed and the strict
`>` test failed, because both runs compiled the full-history branch and the
delete+insert then replaced every key. With the fixture's qualified
`dbt_peerdb.is_incremental()` call, the incremental tests genuinely exercise the
incremental branch: the tie test now depends on `>=` and the strict test proves
the documented miss.

### F6: "no usable snapshot maximum" is unreachable (resolved)

`max()` over a non-null `DateTime64(9)` returns `1970-01-01 00:00:00.000000000`
even for an empty table (observed), and `Nullable(...)` is rejected before the
count query. The snapshot-maximum guard in
`macros/materializations/bucketed_incremental.sql` cannot be triggered through
the public contract; it is kept as defensive code with a comment. The spec
scenario "Rows without a snapshot maximum" was removed, and design.md marks the
error as defensive, so no contract claims testability for it.

## Implemented

### Harness

| Task | File |
|---|---|
| Session-scoped `clickhouse_client` from `tests/conftest.py`, per-test cleanup that drops all relations (tables/views/dictionaries) and the `system.query_log` precondition check | `tests/helpers.py` |
| `run_model(dbt, name, clickhouse_client)` captures dbt events (`capture_events=True`) and every executed statement from `system.query_log` after `SYSTEM FLUSH LOGS`, returns `ModelRun(success, failure_text(), event_messages()/events_matching(), queries_matching())`; both fixtures are passed in by each test, no binder | `tests/helpers.py` |
| `LateWriter` context manager: builds its own client from the `clickhouse_settings` fixture, polls `system.processes` for the sleeping bucket query and inserts a row stamped `now64(9)`; excludes `system.processes` so it cannot match its own poll | `tests/helpers.py` |
| Source DDL/DML helpers, row builders, engine/column/relation inspectors | `tests/helpers.py` |

### Fixture project (`tests/fixtures/dbt`)

- Replaced the example models/seed; removed `tests/test_example.py`.
- `models/sources.yml`: source `bi.bi_source` (schema `default`); all tests shape
  that one table per scenario.
- `macros/bi_model.sql`: shared full-history/incremental model body, calling
  `dbt_peerdb.is_incremental()` (F2), with parameters `watermark_operator`,
  `sleep_seconds`, `include_marker`, `guard_expression`, `detection_flags`.
- 15 behavioural models (`bi_basic`, `bi_defaults`, `bi_strict`,
  `bi_concurrent`, `bi_concurrent_warn`, `bi_ignore`, `bi_failing`,
  `bi_predicates`, `bi_schema_append`, `bi_schema_sync`, `bi_schema_fail`,
  `bi_unique_key_list`, `bi_incremental_detection`, `bi_partitioned`,
  `bi_hooks`) and 19
  validation models (bad identifiers, mismatched `unique_key`, invalid
  `rows_per_bucket`, invalid `on_concurrent_writes`, `inserts_only`,
  non-delete_insert strategies, pre-hook ordering, missing and duplicated
  marker).

### Tests (73 passed, `pytest -q` from the repo root, ~40 s)

| Module | Tests | Covers |
|---|---|---|
| `test_clickhouse.py` | 3 | pinned version, `system.query_log` availability, `ClickHouseColumn` wrapper flags |
| `test_bucketed_incremental_validation.py` | 31 | every error-reference row reachable through the contract; validation before pre-hooks; marker missing/duplicated; source/key/snapshot contract; wrapped keys (`Nullable`, `LowCardinality`); negative keys |
| `test_bucketed_incremental_build.py` | 23 | dedupe, bucket sizing and `<= S0` bounds, defaults, empty source (first run and rebuild), bucket failure leaves target untouched and leftovers are dropped, all six signed and six unsigned integer key types, UUID (`reinterpretAsUInt64`), timezone in the bound, full/incremental/full-refresh path selection, package-qualified incremental detection, `partition_by` applied to the built table |
| `test_bucketed_incremental_publish.py` | 5 | first-run rename, rebuild `EXCHANGE TABLES` and backup drop, view target takes the two-rename path, preexisting tmp/backup dropped, pre/post hooks run |
| `test_bucketed_incremental_incremental.py` | 8 | delete+insert replaces only touched keys, predicates reach the delete, schema append/sync/fail, tie-safe `>=` recaptures an S0 tie, strict `>` misses it, list-form `unique_key` |
| `test_bucketed_incremental_concurrency.py` | 3 | `error` fails and leaves target untouched, `warn` publishes stale then converges on the next incremental run, `ignore` runs no detection query |

### Macro fix

- `macros/materializations/bucketed_incremental.sql`: the key and snapshot probes
  read `col.data_type` for messages and reject `col.is_nullable`/
  `col.is_low_cardinality` (F1).

### Verification

- `export TZ=Africa/Johannesburg`, start ClickHouse 26.3.33.24 via clickhousectl,
  `.venv/bin/pytest` from the repo root: 73 passed.
- `ruff-check --hook-stage manual --all-files` and `ruff-format` pass.
- `pre-commit run --all-files` cannot install `yamlfmt` in this sandbox (no
  network to `proxy.golang.org`); CI only runs `ruff-check`.

## Follow-ups

1. ~~Decide the `is_incremental()` contract and update docs.~~ Done: the documented
   model shape calls `dbt_peerdb.is_incremental()` (`README.md`), and the fixture's
   root-project delegation was deleted. The package macro
   (`macros/materializations/is_incremental.sql`) is retained as the qualified
   target; `spec.md` describes the detection behaviour, not the call syntax.
2. ~~Verify the `data_type` fix against the adapter upgrade path.~~ Done: the probe
   rejects `col.is_nullable`/`col.is_low_cardinality` explicitly, and
   `test_key_column_unsupported_type` covers `LowCardinality(UUID)` and
   `LowCardinality(Nullable(UUID))`. Standing check on each dbt-clickhouse bump:
   confirm `ClickHouseColumn` still parses `Nullable(...)`/`LowCardinality(...)`
   into those flags (and re-wraps `data_type` for messages), then run
   `test_bucketed_incremental_validation.py` and
   `test_clickhouse_column_wrapper_flags`.
3. ~~Restore `system.query_log` assertions where available.~~ Done: the config
   enables `system.query_log`, the harness reads it after `SYSTEM FLUSH LOGS` and
   the server-log parser was removed (see F3).
4. ~~Turn F2 into a regression test.~~ Done: `bi_incremental_detection` emits the
   package-qualified and plain `is_incremental()` results as columns, and
   `test_package_qualified_incremental_detection` asserts `(1, 0)` on an
   incremental run, catching a package rename or a dbt resolution change. The
   qualified call is documented once in `README.md` and specified in
   `spec.md` (Requirement: Incremental Detection, Scenario: Package-qualified
   call).
5. ~~Revisit the unreachable snapshot-maximum error (F6).~~ Done: the guard is
   kept as defensive code with a comment, the spec scenario was removed and the
   design error-reference row is marked defensively unreachable; no test can
   exercise it.
6. **Stabilise the concurrency timings for slower CI.** The fixtures use
   `sleepEachRow(0.5)` with `settings max_threads=1` (two slow statements per
   rebuild, ~35 s for the module). If CI turns out slower, raise the per-row sleep;
   `LateWriter` already fails loudly instead of passing silently (60 s timeout).
7. ~~Add `partition_by` coverage.~~ Done: `adapter.validate_incremental_strategy`
   only consults `partition_by` for `insert_overwrite`, which the macro rejects
   first, so no partition_by value can fail validation. The adapter's create table
   still emits it as `PARTITION BY`, covered by
   `test_partition_by_is_applied_to_the_built_table` with the `bi_partitioned`
   fixture.
8. ~~Clean up unused validation models if they drift.~~ Addressed: `netbek-dw-lib`
   1.36.10 fixed the `--vars` quoting (`json.dumps(vars)` without literal single
   quotes), so per-run config overrides are now possible. Collapsing the 19
   validation models into a table-driven generator stays deferred: dbt hashes
   `--vars` into its partial-parse state (`ManifestStateCheck.vars_hash`), so every
   distinct vars value forces a full re-parse, while the static models are correct
   today and reuse the partial-parse cache. Revisit if the error-reference
   checklist grows enough that file maintenance outweighs the runtime cost.
