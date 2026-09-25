# Plan: integration test suite for `bucketed_incremental`

## Decisions (confirmed)

- Per-scenario model files. `--vars` was unusable when the suite was written (`dw_lib.dbt.Dbt._run_command` wrapped the JSON in literal quotes); netbek-dw-lib 1.36.10 fixed that, but the table-driven generator stays deferred because distinct vars values invalidate dbt partial parsing.
- Source tables created directly with `clickhouse_connect` in tests; no seeds.
- `conftest.py`, `profiles.yml`, CI and mise stay untouched. New fixtures/tests live in `tests/` and `tests/fixtures/dbt/`.

## What is tested

Every requirement and scenario in `docs/bucketed_incremental/spec.md` and every row of the error reference in `docs/bucketed_incremental/design.md`, asserted through observable effects only: target table contents, captured dbt events, `system.query_log` after `SYSTEM FLUSH LOGS`, and relation lifecycle (`system.tables`).

Two scenarios are unreachable through the public contract; the suite documents them instead of faking them:

| Scenario | Why untestable | Treatment |
|---|---|---|
| "Rows without a snapshot maximum" | Non-null `DateTime64(9)` always yields a `max()`; `Nullable(...)` is rejected earlier | Guard kept as defensive code with a macro comment; spec scenario removed and design error row marked defensive (F6) |
| Adapter `validate_incremental_strategy` failure | With `use_lw_deletes: true` the only reachable errors need `unique_key` empty or non-`delete_insert`, both blocked by the macro first; `partition_by` is only consulted for `insert_overwrite`, also blocked | Happy-path runs prove the call happens; `partition_by` DDL coverage comes from `test_partition_by_is_applied_to_the_built_table` |

## File layout

```
tests/
  helpers.py                                  # new: client, dbt-run capture, DDL/DML helpers
  test_bucketed_incremental_validation.py     # new
  test_bucketed_incremental_build.py          # new
  test_bucketed_incremental_publish.py        # new
  test_bucketed_incremental_incremental.py    # new
  test_bucketed_incremental_concurrency.py    # new
  test_example.py                             # delete (replaced by the suite)
  fixtures/dbt/
    dbt_project.yml                           # replace example model config, keep flags
    models/sources.yml                        # source bi.bi_source (schema default)
    models/bucketed_incremental/*.sql         # scenario models (below)
    macros/bi_model.sql                       # shared fixture SQL body
    seeds/my_first_dbt_seed.csv               # delete
    models/example/*, models/example/test_table.yml  # delete
```

## Harness (`tests/helpers.py`)

`BucketedIncrementalTest(IntegrationTest)` inherits the `dbt` fixture from `tests/conftest.py` and adds:

- `clickhouse_client`: the session-scoped fixture from `tests/conftest.py` (a `dw_lib` client built from the dbt profile), requested by every test and passed to `run_model()`/`create_standard_source()`; `clean_database` uses it for the per-test relation drop and the `system.query_log` precondition check.
- autouse `clean_database` fixture: drops every relation in `default` before each test (branch on engine: `View` → `drop view`, `Dictionary` → `drop dictionary`, else `drop table`), so leftover `__dbt_tmp`/`__dbt_backup`/`__dbt_new_data_*` never leak between tests.
- `run_model(dbt, name, clickhouse_client, *, full_refresh=False) -> ModelRun`, capturing, around one `dbt.run(select=name, full_refresh=..., capture_events=True)`:
  - `events`: dbt events (`EventMsg`) collected via the runner callback during the run,
  - `queries`: `system.query_log` statements with `event_time_microseconds >=` a server timestamp taken before the run, read after `SYSTEM FLUSH LOGS`; filtered to initial-query terminal rows and excluding the harness's own queries plus the adapter's `__dbt_exchange_test_*` exchange probe,
  - `result`: the `dbtRunnerResult`.
- `ModelRun.success`, `ModelRun.failure_text()` (concatenates `result.exception` and each `result.results[i].message`; all `raise_compiler_error` messages are substring-matched), `event_messages()`, `events_matching(pattern)`, `queries_matching(pattern)`.
- Source helpers: `create_source(key_type="UInt64", snapshot_type="DateTime64(9)", table="bi_source", include_key/snapshot=True, order_by="tuple()")`, `insert_rows`, `replace_rows`, `fetch_rows`, `query_scalar`, `table_names`, `table_engine`. `Int128`/`Int256` inserts fall back to SQL `insert ... values (toInt128(...))` if the driver rejects native ints.
- Concurrency helper: `late_writer(...)` context manager spawning a thread with its own client, built from `clickhouse_settings`, that polls `system.processes` for a query like `%sleepEachRow%`, then inserts one row stamped with server `now64(9)`; it joins on exit and re-raises any timeout/insert error so the test fails loudly instead of silently passing.

Standard source schema (shared by all models): `id <key_type>`, `payload String`, `_peerdb_synced_at <snapshot_type>`, `_peerdb_is_deleted Int8`, `_peerdb_version Int64`; `MergeTree` `order by tuple()`.

## Fixture project

`models/sources.yml`: `sources: [{name: bi, schema: default, tables: [{name: bi_source}]}]`. Most models read `{{ source('bi', 'bi_source') }}` and set `bucket_source=['bi', 'bi_source']`, so one source name covers those scenarios (tests shape `default.bi_source` per case). A ref-branch fixture reads a plain upstream model through `{{ ref(...) }}` and sets `bucket_ref`, in one single-element and one package-qualified two-element form.

`macros/bi_model.sql` holds the shared body (README model shape): `dbt_peerdb.is_incremental()` branches (package-qualified, because a plain `is_incremental()` resolves to the adapter/global macro, which does not recognise `bucketed_incremental`), `>=` watermark by default, `order by _peerdb_version desc, _peerdb_synced_at desc limit 1 by id`, and the single `-- __BUCKET_PREDICATE__` marker in the full branch. The body takes the relation as an argument and defaults to `{{ source('bi', 'bi_source') }}`; ref models pass `{{ ref(...) }}`, so the ref() call lives in the model body where dbt registers the DAG edge. Parameters: `watermark_operator` (`>=`/`>`), `sleep_seconds` (adds `and sleepEachRow(n) = 0` plus `settings max_threads=1` to the full branch only), `include_marker`, `relation`.

Behavioral models (configs explicit in each file):

| Model | Distinguishing config | Used for |
|---|---|---|
| `bi_basic` | `rows_per_bucket=3`, error | builds, sizing, empty source, incremental, ties, key types, UUID |
| `bi_defaults` | no `rows_per_bucket`/`on_concurrent_writes` | defaults (1 bucket, detection runs) |
| `bi_strict` | `watermark_operator='>'` | strict-watermark contract |
| `bi_concurrent` | sleep 0.5s, error, 1 bucket | concurrent write → error |
| `bi_concurrent_warn` | sleep 0.5s, warn, 1 bucket | concurrent write → warn, later convergence |
| `bi_ignore` | `on_concurrent_writes='ignore'` | no detection query |
| `bi_failing` | guard `throwIf(id = 3, ...)` | bucket failure, target untouched, leftover cleanup |
| `bi_predicates` | `predicates=['id > 100']` | predicates reach delete+insert and are honoured |
| `bi_schema_append` / `bi_schema_sync` / `bi_schema_fail` | `on_schema_change` variants | schema change |
| `bi_unique_key_list` | `unique_key=['id']` | list form accepted |
| `bi_hooks` | `pre_hook` and `post_hook` inserts | hooks run around the build |

Validation models (one bad/edge value each, valid elsewhere; SQL from `bi_model()` or a marker + `select` when the marker itself is the subject): `bi_bucket_key_missing`, `bi_bucket_key_invalid`, `bi_snapshot_missing`, `bi_snapshot_invalid`, `bi_snapshot_equals_key`, `bi_relation_missing_config` (neither key), `bi_relation_invalid_config` (wrong shape), `bi_relation_both_config` (both keys), `bi_unique_key_missing`, `bi_unique_key_mismatch`, `bi_rows_bool`, `bi_rows_zero`, `bi_rows_float`, `bi_on_concurrent_invalid`, `bi_inserts_only`, `bi_strategy_append`, `bi_strategy_legacy`, `bi_bad_hook` (bad `rows_per_bucket` + pre-hook sentinel), `bi_no_marker`, `bi_two_markers`.

## Test matrix

### `test_bucketed_incremental_validation.py` (config + contracts)

| Case | Setup | Assertion (substring unless noted) |
|---|---|---|
| config errors | run each bad model | failure + exact message; no `__dbt_tmp` query in `query_log` |
| validation before hooks | `bi_bad_hook` | sentinel table absent |
| rows bool/zero/float | three models | `rows_per_bucket must be a positive integer` |
| identifier missing/invalid | key and snapshot models | `is required and must be a bare...` |
| relation key set | neither/both/wrong-shape models | `set exactly one of bucket_ref or bucket_source...` / `must be a list or tuple of ...` |
| snapshot == key, unique key missing/mismatch | models | respective messages |
| `on_concurrent_writes`, `inserts_only`, strategy append/legacy | models | respective messages, strategy name included |
| marker missing/duplicated | `bi_no_marker`, `bi_two_markers` | `-- __BUCKET_PREDICATE__ must appear exactly once ... found 0/2` |
| source not found | drop `bi_source` | `not found for model "bi_basic"` |
| source not a table | create view `bi_source` | `must be a table, got type "view"` |
| key column missing / unsupported type (String, `Nullable(UUID)`) | custom source schemas | `not found in bucket_source` / `unsupported type "..."; expected a non-null UUID, signed integer or unsigned integer` |
| snapshot column missing / `DateTime64(3)` / `DateTime` / `Nullable(DateTime64(9))` | custom source schemas | `must be a non-null DateTime64(9) column` |
| negative keys | `Int64` source with one negative id | `has 1 negative values` |

### `test_bucketed_incremental_build.py` (full refresh, sizing, empty, key types)

| Case | Setup | Assertion |
|---|---|---|
| dedupe + contents | 10 keys, one key with 2 versions | target rows == deduped latest versions; row count |
| bucket sizing | 11 rows, B=3 | log `row_count=11 bucket_count=4`, `Processing bucket 1..4` |
| bounds and marker | same run | for each i in 0..3 exactly one bucket query matching `id % 4 = i and _peerdb_synced_at <= toDateTime64(...)`; all four indices present |
| single bucket / defaults | `bi_defaults`, 10 rows | `bucket_count=1` |
| detection query present | `bi_defaults` | one query containing `as writes_detected` |
| empty source | 0 rows | success; target exists empty; log `bucket_count=0`; query contains `where 1 = 0`; no `Processing bucket` |
| empty rebuild | build, truncate, `--full-refresh` | target empty, exchange used |
| bucket failure | build, add poison id, `--full-refresh` | failure; target keeps previous contents; then clean source, re-run `--full-refresh` → success and no `bi_failing__dbt_tmp` (leftover dropped) |
| key types | parametrize all `UInt8..256`, `Int8..256` | success, target holds all ids, bucket query uses bare `id %` |
| UUID key | `UUID` source | success, all rows, `reinterpretAsUInt64(id) %` in bucket query |
| snapshot timezone | `DateTime64(9, 'UTC')` | success; bound literal ends `, 9, 'UTC')` |
| ref branch build | `bi_ref` (1-element) and `bi_ref_package` (2-element) against an upstream table | success; target contents; count and detection queries render the ref relation |
| list and tuple shapes | `bi_ref_tuple`, `bi_source_tuple` | success; target contents |
| ref branch relation contract | ref fixture with missing / view / column-poor upstream | messages name `bucket_ref` and the ref relation |
| incremental detection | first run vs second run vs `--full-refresh` | first/second run log + `query_log` show full branch (`row_count=`) vs incremental branch (`__dbt_new_data_`) vs full branch again |

### `test_bucketed_incremental_publish.py`

| Case | Assertion |
|---|---|
| first run | target is `Table`, no `__dbt_tmp`/`__dbt_backup`; `query_log` has `RENAME TABLE` from tmp, no `EXCHANGE TABLES` |
| rebuild | `EXCHANGE TABLES` present; backup exists in the statement and is gone afterwards; new contents |
| non-atomic path | pre-create a view named `bi_basic`; run without `--full-refresh`; two `RENAME TABLE`, no `EXCHANGE`; target becomes table with model contents |
| preexisting leftovers | create junk `bi_basic__dbt_tmp` and `bi_basic__dbt_backup`; first run succeeds, target correct, junk relations gone |
| hooks | `bi_hooks` inserts a `pre` and a `post` row into a log table; both present after the run |

### `test_bucketed_incremental_incremental.py`

| Case | Assertion |
|---|---|
| delete+insert | update an existing key and add a new one: changed/new keys replaced, untouched key byte-identical; `delete from` + `insert into` + `__dbt_new_data_` in `query_log`; no new-data relation left; target row count |
| no bucketing | `query_log` has no marker predicate / bucket log lines during incremental |
| predicates | update one key `id <= 100` and one `id > 100`; only the latter replaces; delete query contains `and id > 100` |
| schema append | target pre-created without `payload`; `append_new_columns`: success, column added, data present |
| schema sync | target pre-created with obsolete column; `sync_all_columns`: column dropped, missing column added |
| schema fail | mismatch → run fails |
| watermark tie (`>=`) | new version stamped exactly at S0 → next incremental recaptures it |
| strict watermark (`>`) | same setup on `bi_strict` → change stays missed (documented contract price) |

### `test_bucketed_incremental_concurrency.py`

| Case | Assertion |
|---|---|
| error | existing target + `late_writer` during `--full-refresh`: run fails, message contains `concurrent writes ... detected during the rebuild` and `previous table was left untouched`; target contents unchanged |
| warn | run succeeds; log `WARNING: concurrent writes ...`; late row absent from published target; subsequent incremental run (no writer) converges and includes it |
| ignore | run succeeds; `query_log` contains no `as writes_detected` query |

## Implementation order

1. Trim/replace fixture project: `dbt_project.yml`, `sources.yml`, delete `models/example/*` and the example seed, add `macros/bi_model.sql`.
2. Add behavioral + validation models listed above.
3. Add `tests/helpers.py` (client, cleanup, `run_model`, query-log/log capture, `late_writer`).
4. Add the five test modules, validation first.
5. Delete `tests/test_example.py`.
6. Run the suite, then `make lint`.

## Verification

```shell
.venv/bin/clickhousectl local server start --version 26.3.33.24 --http-port 18123 --tcp-port 19000
.venv/bin/pytest -s tests/test_bucketed_incremental_validation.py
.venv/bin/pytest -s
.venv/bin/clickhousectl local server stop
```

Also `make lint` (or `pre-commit run ruff-check --hook-stage manual --all-files`). Expected suite runtime on the local node: roughly 2–4 minutes (~60 dbt invocations), plus ~15–30 s for the sleep-based concurrency cases.

## Risks and mitigations

- **Concurrency flake**: bucket zero is slowed with `sleepEachRow(0.5)` and `settings max_threads=1` (server caps at 3 s/row); the writer polls every 20 ms and the helper raises on timeout instead of passing silently.
- **`clickhouse__create_table_as` emits CREATE EMPTY AS SELECT + INSERT** (`dbt/include/clickhouse/macros/materializations/table.sql:266`), so bucket 0's predicate may appear twice in `query_log`; assertions use sets of bucket indices, not statement counts.
- **Event capture**: `run_model` passes `capture_events=True` so `dbtRunner` collects `EventMsg`s via its callback; `events_matching()` filters the `.msg` text (e.g. `JinjaLogInfo` for the `info=True` log lines). No file-log reads.
- **`system.query_log` disabled**: the `clickhouse` fixture asserts the table exists and `log_queries = 1` up front and fails with a clear message.
- **Timezone-rendered bounds**: never assert exact timestamp strings except the `'UTC'` suffix test; rely on regex + bucket index sets.
- **Tests share one database**: no pytest-xdist; the autouse cleanup drops all relations before each test, and concurrency threads are joined via the context manager.
