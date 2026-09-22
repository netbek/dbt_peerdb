# Integration test suite plan: `bucketed_incremental`

## 1. Goal

A pytest suite that exercises the `bucketed_incremental` materialization end-to-end against the real ClickHouse server (started via `clickhousectl` per `AGENTS.md`), covering every scenario in `docs/bucketed_incremental/spec.md` and the error reference in `design.md`, using the existing `IntegrationTest` / `Dbt` / `ClickHouseAdapter` harness unchanged.

## 2. Test architecture

### 2.1 Layout

```
tests/
  conftest.py                          # unchanged (IntegrationTest, Dbt, ClickHouseAdapter)
  bucketed_utils.py                    # NEW: helpers (not collected by pytest)
  test_bucketed_incremental.py         # NEW: all test classes in one module (keeps the suite navigable)
  fixtures/
    .dbt/profiles.yml                  # unchanged (use_lw_deletes: true already set)
    dbt/
      dependencies.yml                 # unchanged (local package path)
      dbt_project.yml                  # updated: drop example model path config; set vars defaults
      models/bucketed/
        bucketed_model.sql             # NEW: one parametrized model, config from --vars
      (remove models/example/*, seeds/my_first_dbt_seed.csv, test_example.py)
```

Rationale for a **single test module**: keeps the suite navigable with helpers in `bucketed_utils.py`. ClickHouse runs externally via `clickhousectl`, so there is no container lifecycle tied to test modules.

`test_example.py` is removed: its models/seeds are replaced, so it would fail.

### 2.2 Parametrized model (design §Test strategy)

One model, all knobs via `--vars` (`Dbt.run(vars=...)` already supported):

| Var | Purpose |
|-----|---------|
| `bucket_key_column`, `bucket_snapshot_column`, `bucket_source_table`, `unique_key` | config under test (defaults = valid baseline) |
| `rows_per_bucket`, `on_concurrent_writes`, `inserts_only`, `incremental_strategy`, `on_schema_change` | config under test |
| `marker_mode`: `normal \| missing \| duplicate` | marker count scenarios |
| `watermark_op`: `>= \| >` | tie-safe vs strict watermark contract |
| `sleep_ms` | injects `sleepEachRow(...)` into the full-history select (slows bucket 0 for the mid-build writer) |
| `fail_id` | injects `throwIf(id = <fail_id>, 'injected')` (bucket-failure test) |
| `include_extra` | selects an extra source column (schema-change test) |

Model body follows the README example: `is_incremental()` branch with `changed_keys` using `{{ var('watermark_op', '>=') }}`, full-history branch containing the marker. Static config: `materialized="bucketed_incremental"`, `engine="MergeTree()"`, `order_by` on the key.

### 2.3 Source tables: adapter DDL, not seeds

`bucket_source_table` must be a real table with exact dtypes (`DateTime64(9)`, `UUID`, `Int256`, `DateTime64(9, 'UTC')`, `Nullable(...)` rejections). Seeds cannot express these reliably. Helpers create sources via `ClickHouseAdapter.create_client()` DDL + batched `INSERT` (per `insert-batch-size`, keep batches modest — fixtures are ≤ a few thousand rows; per `schema-types-native-types` use native types; per `schema-types-avoid-nullable` sources under test are non-null by contract, `Nullable` only in negative tests).

### 2.4 Isolation

Autouse fixture per test:

1. Record `logs/dbt.log` size (run-log slice boundary).
2. Record `system.query_log` time boundary.
3. Drop `bucketed_model` and any `bucketed_model__dbt_tmp` / `__dbt_backup` / `__dbt_new_data_%` relations; drop that test’s source tables (and any `src_view`, helper DBs).

Each test builds its own source with a unique name or a fresh drop/create.

### 2.5 Assertion surfaces

| Surface | How |
|---------|-----|
| Success / failure | `runner_result.success`; on failure, node `message` (or sliced log) contains `bucketed_incremental: …` |
| Run logs | read only bytes appended to `fixtures/dbt/logs/dbt.log` since the boundary (`row_count=`, `bucket_count=`, `Processing bucket`, `WARNING: concurrent writes`) |
| Table contents | `client.query(...)` compare ordered rows vs expected |
| Relation state | `system.tables` / `adapter.has_table`; leftover `__dbt_tmp` / `__dbt_backup` absence |
| `system.query_log` | design requires it (bucket predicates, `EXCHANGE TABLES`, detection-query counts) |

**Enabling `log_queries` without server config changes**: a session fixture runs `ALTER USER default SETTINGS log_queries = 1` once per module, then `SYSTEM FLUSH LOGS` before each query_log read. Filters: `query_start_time >= test boundary` AND query text contains a distinctive fragment (model/source name). This fulfills the design's “log_queries on” requirement inside test code, leaving server config untouched.

### 2.6 Mid-build writer (design’s concurrency test)

- Main thread: `dbt run` (full refresh, `sleep_ms > 0` so bucket 0 takes seconds).
- Background thread: poll `system.processes` until a query matching the source table + `sleepEachRow` appears (proves the count query already captured `S0`), then `INSERT` a row with `now64(9)` (stamp `> S0`).
- Writer waits with a hard timeout (~30s) so a hang fails loudly rather than deadlocking.

Deterministic: write always lands between count and detection, per design §Test strategy.

### 2.7 Helpers in `bucketed_utils.py`

- `valid_vars(**overrides)` — baseline valid config; tests override one field.
- `make_source(ddl, rows)` / `query_all(sql)` / `table_rows(name)`.
- `run_model(dbt, *, expect_error: str | None = None, **vars)` — wraps `dbt.run(select=..., vars=...)`, returns result; asserts message substring on failure.
- `log_since(boundary)` — sliced dbt.log text.
- `query_log_since(boundary, contains)` — flushed + filtered query_log rows.
- `assert_no_leftovers(dbt_name)` / `assert_relation_kind`.
- `dedupe_expected(rows)` — expected target = source deduped by key (latest version wins), used by every content assertion.

## 3. Test matrix

Baseline for every test: otherwise-valid config, unique source table, expected content = deduped source.

### A. Configuration validation (spec: Configuration Validation, Strategy Enforcement) — fails **before** DB work and **before** pre-hooks

| # | Scenario | Assert |
|---|----------|--------|
| A1 | `bucket_key_column` missing / `"id; x"` / `"id-1"` / `""` | error `bucket_key_column is required…` |
| A2 | `bucket_snapshot_column` missing / non-identifier | matching error |
| A3 | `bucket_snapshot_column == bucket_key_column` | `must be a different column` |
| A4 | `bucket_source_table` missing / `"src"` / `"default.src;--"` / `"default.src x"` | `form "database.table"` error |
| A5 | `unique_key` missing or `other_col` | `unique_key must be the single bucket_key_column` |
| A6 | `rows_per_bucket` ∈ {`0`, `-1`, `1.5`, `"abc"`, `true`} (parametrize) | `rows_per_bucket must be a positive integer` |
| A7 | `on_concurrent_writes="fail"` | enum error |
| A8 | `inserts_only=true` | `inserts_only is not supported` |
| A9 | `incremental_strategy="append"` (and `"legacy"`) | `only the delete_insert…` naming resolved strategy |
| A10 | Validation ordering: pre-hook writes a marker row; trigger A1 on a run with that pre-hook | marker row **absent** (validation precedes pre-hooks) |
| A11 | Any A-case while a good target already exists | target contents unchanged |

### B. Marker (spec: Bucket Predicate Marker) — full refresh only

| # | Scenario | Assert |
|---|----------|--------|
| B1 | `marker_mode=missing` | `marker -- __BUCKET_PREDICATE__ must appear exactly once… found 0`; no bucket queries in query_log since boundary |
| B2 | `marker_mode=duplicate` | `…found 2`; no bucket queries |
| B3 | Incremental run on the normal model | succeeds; compiled/run SQL path has no marker check (success is the assertion; full-refresh-only check) |

### C. Source & dtype contract (spec: Key Column Contract, Source Table Contract, Snapshot Column Contract) — full refresh probe

| # | Scenario | Assert |
|---|----------|--------|
| C1 | `bucket_source_table` names a nonexistent table | `not found for model` |
| C2 | Source is a view | `must be a table, got type "view"` |
| C3 | Key column absent from source | `bucket_key_column "…" not found` |
| C4 | Key dtype ∈ {`String`, `Nullable(Int64)`, `DateTime`, `Float64`, `Decimal(18,2)`} (parametrize) | `unsupported type` naming column + dtype |
| C5 | Snapshot column absent | `bucket_snapshot_column "…" not found` |
| C6 | Snapshot dtype ∈ {`DateTime64(3)`, `DateTime`, `Nullable(DateTime64(9))`, `Date`} (parametrize) | `must be a non-null DateTime64(9)` |
| C7 | Integer key source contains negative values | `has <n> negative values`, n correct |
| C8 | Existing target + C7/C4 failure | target untouched |

Not reachable end-to-end: “rows without usable snapshot maximum” — requires rows + null max, but the dtype check already rejects `Nullable`. Skip; note as defense-in-depth.

### D. Full refresh (spec: Bucketed Full Refresh, Bucket Sizing, Empty Source, Atomic Publication)

| # | Scenario | Assert |
|---|----------|--------|
| D1 | First run, N rows, default `rows_per_bucket` → 1 bucket | success; content == expected; kind = table; no leftovers |
| D2 | 100 rows, `rows_per_bucket=30` → `ceil=4` buckets | log `row_count=100 bucket_count=4`; ≥4 bucket statements in query_log; content exact (each key exactly once ⇒ partition correct) |
| D3 | `rows_per_bucket >= N` → 1 bucket | log `bucket_count=1` |
| D4 | Empty source | success; `row_count=0 bucket_count=0`; target exists with 0 rows; no detection query needed (snapshot empty ⇒ skipped) |
| D5 | UUID key, multi-bucket | success; all UUIDs present once (exercises `reinterpretAsUInt64` path) |
 D6 | Key dtype ∈ {`Int8`, `Int64`, `UInt8`, `UInt64`, `UInt256`} (parametrize, small) | success + exact content (bare-column modulo path) |
| D7 | Snapshot `DateTime64(9, 'UTC')` | success; bucket predicates in query_log keep `'UTC'` in the `toDateTime64(...)` literal |
| D8 | Every bucket query carries `<= S0` | query_log: all bucket/CTAS queries matching the source contain `<=` bound and `% <N> = <i>` (spec: Snapshot bound / Marker replaced) |
| D9 | Rebuild `--full-refresh` over existing table | content replaced; query_log contains `EXCHANGE TABLES` (Atomic DB ⇒ `can_exchange` path); no `__dbt_backup` left |
| D10 | Bucket failure mid-rebuild (`fail_id` set, key exists in a later bucket, target pre-built) | run fails with injected error; **target keeps previous contents**; leftover `__dbt_tmp` may remain (documented) |
| D11 | First-run failure (D10 with no prior target) | failure ⇒ target **absent** |
| D12 | Preexisting `bucketed_model__dbt_tmp` and `__dbt_backup` tables | run succeeds; both gone afterward |
| D13 | Existing relation is a **view** with the model name | treated as full refresh; final relation is a table |
| D14 | Run-log line present after count (spec: Run log) | `row_count=… bucket_count=… snapshot_max=…` in sliced log |

### E. Concurrent-write detection (spec: Concurrent-Write Detection)

Shared setup: build target once against a quiesced source; start rebuild with `sleep_ms` > 0; writer thread inserts post-`S0` row when bucket query appears.

| # | Mode | Assert |
|---|------|--------|
| E1 | default `error` (rebuild) | run fails; message `concurrent writes… snapshot bound … Re-run against a quiesced source`; target contents == pre-rebuild contents; writer row absent |
| E2 | `error` on **first run** | failure; target never created |
| E3 | `warn` | run succeeds; `WARNING: concurrent writes` in sliced log; publish happened (target replaced) but writer row absent (converges on a later incremental) |
| E4 | `ignore` | run succeeds; query_log since boundary has **no** detection query (`writes_detected` / `max(...) > toDateTime64` pattern absent); bucket queries still bounded |
| E5 | `error` + no mid-build write | run succeeds (detection false ⇒ no false positive) |

query_log: detection queries (E1/E3/E5) ≈ 1; E4 = 0 (spec: “one additional max() scan” / ignore skips).

### F. Incremental path (spec: Incremental Maintenance, Incremental Detection, Model Contract)

| # | Scenario | Assert |
|---|----------|--------|
| F1 | First run then plain second run with **no source change** | both succeed; `is_incremental()` true on 2nd (content stable; no marker check on 2nd run) |
| F2 | Insert new version of key K (higher `_peerdb_synced_at`/`_peerdb_version`); incremental | only K’s row replaced; other keys byte-identical; no `__dbt_new_data_` tables left |
| F3 | Tombstone version (`_peerdb_is_deleted=1`) for K | target row is the tombstone (delete via replace) |
| F4 | **Tie-safe watermark**: key A stamped `t0`, key B sets `W=t1`; new version of A stamped exactly `t1`; incremental with `>=` | A recovered (new payload present) |
| F5 | Same as F4 with `watermark_op='>'` | A **not** recovered (documents the unenforceable strict contract) |
| F6 | Key touched selection re-reads **all** versions: A has v1(<W), v2, v3(≥W) | target has v3 (model contract pattern works with delete+insert) |
| F7 | Path selection proof: delete a row from target manually; incremental (source unchanged) | row **stays missing** (source untouched ⇒ not re-selected); `--full-refresh` | row **restored** |
| F8 | `on_schema_change=append_new_columns`: build; `ALTER TABLE source ADD COLUMN extra`; rerun with `include_extra=true` | success; target has `extra` column and correct values |
| F9 | `on_schema_change` default `ignore` + new column selected only in model… (or source-only change without model change) | success; target schema unchanged |
| F10 | `predicates=["id >= 0"]` (harmless) on incremental | success (plumbing reaches `validate_incremental_strategy` + delete+insert) |
| F11 | Full refresh after incremental (`--full-refresh`) | bucket path again; content = full deduped source (F7’s second half already covers; keep if F7 split) |

### G. Lifecycle / publish leftovers (cross-cutting, asserted in D/E/F as noted)

- After every successful run: no `__dbt_tmp`, no `__dbt_backup`, no `__dbt_new_data_%`.
- After failed rebuild: target intact; after failed first run: no target.

## 4. Implementation order

1. **Fixture project**: write `models/bucketed/bucketed_model.sql`; delete `models/example/*`, `seeds/my_first_dbt_seed.csv`, `tests/test_example.py`; adjust `dbt_project.yml` (remove example model path config; optional `vars:` defaults).
2. **`bucketed_utils.py`**: baseline vars, DDL/insert helpers, `run_model`, log/query_log slicing, expected-content helper, leftover asserts, `log_queries` user-setting fixture.
3. **`test_bucketed_incremental.py`**, classes in dependency order:
   - `TestConfigValidation` (A) — fastest, no data needed beyond a dummy source for cases that get past pure-config checks (A1–A9 fail before DB; only A10/A11 need a pre-built target).
   - `TestMarker` (B)
   - `TestSourceContract` (C)
   - `TestFullRefresh` (D1–D8, D12–D14)
   - `TestPublishFailure` (D9–D11)
   - `TestConcurrentWrites` (E)
   - `TestIncremental` (F)
4. Start ClickHouse, run the suite, then stop the server (per `AGENTS.md`; `TZ` always set; port `18123` matches `profiles.yml`):
   ```shell
   TZ=Africa/Johannesburg .venv/bin/clickhousectl local server start --version 26.3.33.24 --http-port 18123 --tcp-port 19000
   .venv/bin/pytest -s tests/test_bucketed_incremental.py
   # then the full suite plus `ruff`/`ty` per repo config
   .venv/bin/pytest -s
   TZ=Africa/Johannesburg .venv/bin/clickhousectl local server stop
   ```
5. CI needs no changes (ClickHouse provisioned via `clickhousectl` + `.venv/bin/pytest` already wired; no `docker` / `pytest-docker` dependencies).

## 5. Risks and decisions

| Risk | Mitigation |
|------|------------|
| `log_queries` off by default on the `clickhousectl` server | `ALTER USER default SETTINGS log_queries=1` in a module fixture + `SYSTEM FLUSH LOGS`; no server config edit |
| Concurrency test flake | Writer keyed off `system.processes` seeing the bucket query; generous `sleepEachRow`; hard 30s writer timeout |
| Single test file size (~1k lines) | Classes per spec layer; helpers externalized; accepted for navigability |
| C8/unreachable “no usable maximum” | Excluded; noted in module docstring as unreachable under current dtype validation |
| `1.0` as `rows_per_bucket` | Macro accepts whole floats (`1.0|int == 1.0` is false); do **not** assert it errors; use `1.5` for the non-integer case |
| query_log cross-test bleed | Time-window filter + distinctive query fragments |
| Design D8: no resumability | Not tested (non-goal) |

## 6. Out of scope

- Grants / `persist_docs` / indexes / contracts beyond “run still succeeds” (spec: unchanged lifecycle configs).
- Adapter-internal behavior of `clickhouse__incremental_delete_insert` beyond observable table state.
- Resumable builds, physical mid-build deletes, backdated-stamp recovery (design non-goals / residual risk).
- `tasks.md` — not read (per your instruction); plan derived only from `design.md`, `spec.md`, the macro, and the harness.

## 7. Files touched at implementation time

| Action | Path |
|--------|------|
| Add | `tests/bucketed_utils.py` |
| Add | `tests/test_bucketed_incremental.py` |
| Add | `tests/fixtures/dbt/models/bucketed/bucketed_model.sql` (+ optional schema yml) |
| Edit | `tests/fixtures/dbt/dbt_project.yml` |
| Delete | `tests/test_example.py`, `tests/fixtures/dbt/models/example/*`, `tests/fixtures/dbt/seeds/my_first_dbt_seed.csv` |
| Unchanged | `tests/conftest.py`, `profiles.yml`, CI, `dependencies.yml`, macros |
| Prerequisite (not a repo file) | ClickHouse `26.3.33.24` via `clickhousectl` (`TZ` always set), ports `18123`/`19000` |
