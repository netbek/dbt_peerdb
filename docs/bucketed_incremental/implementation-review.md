# bucketed_incremental implementation review

Date: 2026-09-23.
Scope: `docs/bucketed_incremental/{spec,design,concurrent-writes-data-loss,integration-test-plan,integration-test-implementation}.md` against `macros/materializations/{bucketed_incremental.sql,is_incremental.sql}` and `tests/test_bucketed_incremental_*.py` plus `tests/helpers.py` and `tests/fixtures/dbt`.

Method: read-only inspection. No test runs. Line references are to `macros/materializations/bucketed_incremental.sql` unless stated.

Skills applied: `using-dbt-for-analytics-engineering`, `fetching-dbt-docs`, `clickhouse-best-practices`.

## 1. Verdict

The implementation matches the spec and design on all reachable paths. Validation, bucket sizing, snapshot pinning, detection, publish, and incremental maintenance behave as specified. The test suite covers every reachable error row and every spec scenario except documented intentional gaps.

Seven correctness and robustness issues remain. None invalidates the current design. Two deserve fixes: validation ordering and float ceiling. The rest are documentation, guardrails, or extra tests.

## 2. Spec conformance

| Spec requirement | Implementation | Status |
|---|---|---|
| Incremental detection: table + `bucketed_incremental` + not full refresh | `is_incremental.sql:2-12`; `bucketed_incremental.sql:17` `full_refresh_mode` | Pass. Pinned by `test_package_qualified_incremental_detection`. Plain `is_incremental()` correctly returns false. |
| Bucketed full refresh into intermediate, publish only after all buckets | `L200-201, L254-284, L321-332` | Pass. Failure before publish leaves target untouched (`test_bucket_failure_leaves_target_untouched_and_cleans_up`). |
| Marker exactly once, replaced per bucket with `<key> % N = i` plus `snapshot <= S0`; no check on incremental | `L105-111, L261-271`; incremental branch `L308-319` skips check | Pass. `test_marker_missing`, `test_marker_duplicated`, `test_bucket_sizing_and_snapshot_bounds`. |
| Sizing `ceil(R/B)`, default `100000`, minimum 1, positive-integer check, bool rejected, run log | `L20, L67-71, L244-253` | Pass with one precision caveat, section 4.1. |
| Empty source builds `where 1 = 0` and publishes | `L254-258` | Pass. Both first-run and rebuild covered. |
| Config validation before database work and pre-hooks | `L35-94` before `L96-100` | Mostly pass; ordering gap in section 4.2. |
| Strategy must resolve to `delete_insert`; call `validate_incremental_strategy`; reject `inserts_only` | `L78-94` | Pass. `bi_strategy_append`, `bi_strategy_legacy`, `bi_inserts_only` covered. |
| Key contract: infer UUID/signed/unsigned, reject nullable/wrapped/other, reject negatives, require column exists | `L128-173, L202-224` via `col.data_type` + `is_nullable`/`is_low_cardinality` | Pass. F1 fix verified by `test_key_column_unsupported_type` and `test_clickhouse_column_wrapper_flags`. |
| Source must exist as table | `L112-127` | Pass. |
| Snapshot must be non-null `DateTime64(9)` with optional timezone; every bucket `<= S0`; literal keeps timezone | `L174-187, L242, L261-270` | Pass. `test_snapshot_timezone_is_kept_in_bound`. Regex strictness noted in section 4.5. |
| Detection `max > S0` after loop unless `ignore` or empty bound; `error` stops before publish, `warn` logs, `ignore` skips query | `L286-306` | Pass. All three modes covered. |
| Publish: rename first run; exchange if `can_exchange`; two renames otherwise; drop leftovers first; drop backup after commit | `L96-97, L321-332, L346-350` | Pass. All three paths covered. |
| Incremental: delete+insert via adapter, predicates passthrough, schema change before step | `L309-318, L92` | Pass. Predicate alias gap in section 6. |
| Model contract: one row per key, `>=` watermark, all versions of touched key | Not enforced by macro; documented in README/spec | Pass by documentation. `>=` vs `>` both tested. |

## 3. ClickHouse rules checked

Per `clickhouse-best-practices`: cite rule or state source.

- `schema-pk-filter-on-orderby`: tradeoff accepted. `id % N = i` is non-sargable and defeats the sparse index on `ORDER BY id`. Each bucket therefore full-scans the source. Design D9 accepts `N` scans for bounded memory. Compliant by explicit tradeoff; record it where `rows_per_bucket` is documented.
- `schema-types-native-types`, `schema-types-minimize-bitwidth`, `schema-types-avoid-nullable`: compliant. Tests use `UInt64`, `String`, `DateTime64(9)`; nullable/wrapped keys and snapshots are rejected.
- `schema-partition-low-cardinality`: resolved by example. `partition_by` passes through unchecked, so the fixture partitions by the low-cardinality `region` (`LowCardinality(String)`, 3 values) rather than `id`; the design config table carries the cardinality warning.
- `insert-batch-size`: note. Default `rows_per_bucket=100000` yields about 100K rows per bucket INSERT, at the top of the 10K-100K range that avoids small parts while bounding peak memory per bucket.
- `insert-mutation-avoid-delete`, `insert-mutation-avoid-update`: compliant. Incremental path requires lightweight deletes (`use_lw_deletes: true`) and rejects `legacy`/`append`/`insert_overwrite`.
- `insert-optimize-avoid-final`, `query-join-*`: no violation. Dedupe uses `LIMIT 1 BY id`; delete+insert shape is inherited from the adapter.

dbt practice per `using-dbt-for-analytics-engineering`: models use `source()` for raw PeerDB tables, CTEs over subqueries, package-qualified `dbt_peerdb.is_incremental()`, per-scenario fixture models to preserve partial parsing. Compliant.

## 4. Edge cases in the macro

### 4.1 Bucket count uses float division

`L244`: `((row_count / rows_per_bucket) | round(0, 'ceil')) | int`.

Jinja `/` is float division. Beyond `2**53` rows the quotient loses integer precision and `ceil` can be off by one, producing the wrong `N`. Row counts that large are not expected today, but the fix is trivial: integer arithmetic `(row_count + rows_per_bucket - 1) // rows_per_bucket`.

Related: `range(bucket_count)` at `L260` is unbounded. `rows_per_bucket=1` on a large source would schedule millions of statements. Consider a maximum bucket count or a minimum effective `rows_per_bucket` with a clear error.

### 4.2 Drops and pre-hooks run before late validation

Order today: config checks `L35-94`, drops `L96-97`, pre-hooks `L99-100`, then marker `L105-111`, source probe `L112-127`, dtype checks `L144-186`, negative keys `L215-223`.

A model that fails marker, source, dtype, or negative-key validation has already dropped `__dbt_tmp`/`__dbt_backup` and run pre-hooks. The existing test `test_validation_runs_before_hooks` uses `bi_bad_hook` with a bad `rows_per_bucket`, which fails in the early block, so it does not catch this. Move drops and pre-hooks after all validation that can raise before any bucket work.

### 4.3 Predicate assumes marker is the first filter

`L261-271` always emits `where <lhs> % N = i and <snap> <= S0`. The fixture places the marker where a `WHERE` belongs, with optional `and sleepEachRow` after it. If a model already has `WHERE x` before the marker, replacement yields `WHERE x where mod ...`, which fails at runtime. Either detect a preceding `WHERE` or state in spec and README that the full-history branch must have no `WHERE` before the marker and must append further conditions with `AND` after it.

### 4.4 Marker matching is exact string

`L105` uses `sql.count(marker)` with `marker = '-- __BUCKET_PREDICATE__'`. Whitespace or case variants fail closed, which is correct. A marker inside a string literal is still counted and then `replace` corrupts the literal. Comment/literal-aware parsing is overkill; document that the marker must appear as a SQL comment exactly once.

### 4.5 Snapshot type regex is strict

`L180`: `^DateTime64[(]9(, *'[^']+')?[)]$`. Correct for canonical adapter output (`DateTime64(9)`, `DateTime64(9, 'UTC')`). It would reject lowercase or oddly spaced variants if the adapter ever returned them. Safe today; re-check on each dbt-clickhouse bump alongside the existing `test_clickhouse_column_wrapper_flags` check from F1. Timezone extraction `L187` via `split("'")[1]` is safe because ClickHouse timezone names contain no quotes, and `[^']+` blocks injection.

### 4.6 Empty source still runs detection

An empty non-null `DateTime64(9)` returns `max` as epoch (F6), so `snapshot_str` is not none and `L286` runs the `max > S0` query even when `row_count == 0`. Harmless and it correctly catches a late insert, but skipping detection when `row_count == 0` would save one scan and match the spec phrase about an empty captured maximum.

### 4.7 Detection truthiness and blind spots

`L290` `if writes_detected` handles `0/1` and `True/False`. `NULL`/`None` is falsy and means no warning, which is reasonable.

Detection compares maxima only, so it cannot see truncates, TTL expiry, or physical deletes that lower the source without raising `max`. That matches the documented quiesce requirement for rebuilds, but the `error` mode message should not imply all concurrent changes are caught. Suggested tweak: “snapshot bound exceeded” already says this correctly; keep it and avoid broader wording.

A dropped source mid-build raises the adapter error rather than the fail-closed message. Target remains untouched because the error precedes publish, which is the property that matters.

### 4.8 `unique_key` normalization gaps

`L7-13, L61`: `None`, `[]`, `""` correctly become `none` and error; `['id']` joins to `"id"` and passes; multi-column lists correctly fail. Two gaps: `unique_key` is never trimmed while `bucket_key_column` is (`L41`), so `" id "` falsely rejects; a scalar non-string such as `123` hits `|length` with `TypeError` instead of `raise_compiler_error`. Guard type before length and trim both sides.

### 4.9 Predicates and partition passthrough

`L92`: `config.get('predicates', []) or config.get('incremental_predicates', [])`. When both are set, `predicates` silently wins. Warn on both-set or document precedence. The `incremental_predicates` alias has no test; only `predicates=['id >= 0']` is covered. `partition_by` reaches `validate_incremental_strategy` at `L94` but is not passed to `clickhouse__incremental_delete_insert` at `L316`; verify the callee signature on adapter upgrades.

### 4.10 Publish branches

`L321-332`: first run renames; `can_exchange` exchanges; otherwise two renames. A pre-existing view takes the two-rename path and becomes a table, covered by `test_view_target_uses_two_renames`. `need_swap` is true only on full refresh, so incremental runs mutate in place as intended. The two-rename window where readers can miss the target is documented in design.

## 5. Source and key probing

- The bucket relation comes from `ref()`/`source()`; the probe passes `bucket_relation.database`, `bucket_relation.schema` and `bucket_relation.identifier` to `adapter.get_relation`. ClickHouse ignores `database` and aliases schema to database, but the macro splits no strings and assumes no string shape.
- `countIf(key < 0)` is emitted for both signed and unsigned integers. For `UInt*` it is always zero: one wasted aggregate inside an otherwise single-pass `count(), countIf, max()` query. Leave as is or restrict to signed types.
- `snapshot_str` roundtrip `toString(max) -> toDateTime64('S0', 9[, TZ])` preserves 9 digits and timezone. DST fold ambiguity in string roundtrip is a one-hour edge in a narrow window; acceptable and narrower than the documented backdate residual.
- Watermark provenance: PeerDB `_peerdb_synced_at` is destination-stamped at normalize time (monotonic per node), so backdates need skew, step-back, or manual writes; own updated-at columns are source-stamped, making backdates routine. Operator docs (README, design, concurrent-writes doc, spec note, macro comment, T9 test docstrings) now state the split explicitly.

## 6. Test coverage gaps

No planned case from `integration-test-plan.md` is unimplemented. Deltas are supersets (`LowCardinality` variants, `bi_incremental_detection`, `bi_partitioned`). Intentional non-coverages: F6 snapshot-maximum guard and `validate_incremental_strategy` failure injection.

Gaps worth closing, in priority order:

1. `incremental_predicates` alias and both-set precedence. Spec allows both; only `predicates` tested.
2. Multi-column `unique_key` list must fail; empty-list normalization path untested.
3. Sizing shapes beyond `11/3=4` and single-bucket default: exact division, `rows_per_bucket > R`, `rows_per_bucket=1` small source.
4. Detection count assertion in `error`/`warn` modes (currently only `defaults` asserts exactly one `writes_detected`, `ignore` asserts zero).
5. Incremental-run marker exemption asserted directly on compiled SQL, not only indirectly via absence of bucket logs.
6. Documentary backdate test: write stamped below `S0` after its bucket stays missed, proving the stated residual.
7. Negative `rows_per_bucket`, string value, and `rows_per_bucket > R` single-bucket shape.
8. Harness brittleness to record: `LateWriter` triggers only on `sleepEachRow`; `__dbt_exchange_test` filter depends on adapter probe name; log tail relies on `logs/dbt.log` flush; suite is serial-only on shared `default` database.

## 7. Tasks

### P0 — correctness

- [x] T1 Move `drop_relation_if_exists` and `run_hooks(pre_hooks)` after marker, source, dtype, and negative-key validation. (Not fixing: same ordering as upstream `incremental.sql:25-29` before strategy/schema validation; reordering locally would diverge from upstream and complicate merges. Side effect is limited to staging drops plus pre-hooks; leftovers are cleaned by the next successful run.)
- [x] T2 Replace float ceiling with integer arithmetic. Acceptance: unit-level Jinja check plus existing sizing test `11/3=4` still passes; add exact-division case. (Done 2026-09-23: `bucket_count = ((row_count + rows_per_bucket - 1) // rows_per_bucket) | int`; `test_bucket_sizing_and_snapshot_bounds` and full build module 23 passed.)
- [x] T3 Harden `unique_key`: type-guard before `|length`, trim before compare. (Not fixing: normalization block is copied verbatim from upstream `incremental.sql:6-12`; hardening locally would diverge from upstream. Mis-typed `unique_key` still fails loudly at compile time; proper fix belongs upstream in dbt-clickhouse.)

### P1 — contract clarity

- [x] T4 Document marker placement: no `WHERE` before marker; extra full-branch conditions go after marker with `AND`. (Done 2026-09-24, doc-only: spec Marker requirement + replaced scenario, design D1 consequences, README example comment + full-history bullet, macro replace-site comment. No fixture — a misplaced `WHERE` fails as a DB syntax error with no macro-level message to assert. Marker/config/sizing spot-checks passed.)
- [x] T5 Document `predicates` vs `incremental_predicates` precedence and warn or error when both are set. (Not fixing: expression is identical to upstream `incremental.sql:60`; erroring or changing precedence locally would change behavior versus upstream incremental models. Keep parity; precedence stays `predicates` wins. Proper fix belongs upstream in dbt-clickhouse.)
- [x] T6 Clarify detection scope in docs: catches writes raising `max` above `S0`; does not catch truncates or physical deletes. (Done 2026-09-24, doc sentences only in design fail-closed section, spec detection requirement, hazard-doc macro summary, README intro; macro message wording unchanged, still anchored on snapshot bound. Concurrency module 3 passed.)
- [x] T7 Record adapter assumptions: canonical `data_type` casing/spacing, `get_relation(database=schema)`, `__dbt_exchange_test` probe name, `ClickHouseColumn` wrapper flags. (Not fixing in code: assumptions are shared with upstream exchange/column paths and already tracked as a checklist via `integration-test-implementation.md` F1/F6 plus `test_clickhouse_column_wrapper_flags`; re-check on each dbt-clickhouse bump.)

### P2 — tests and guardrails

- [x] T8 Coverage: multi-column/empty-list `unique_key` rejection, `incremental_predicates` alias, exact-division and B=1 sizing, detection count in error/warn, incremental marker absence, `rows_per_bucket` negative/string. (Done 2026-09-24: 6 fixtures — `bi_unique_key_multi/empty`, `bi_incremental_predicates`, `bi_one_per_bucket`, `bi_rows_negative/string`; 4 config cases + 3 new tests + extended assertions; full suite 81 passed, `ruff-check` + `ruff-format` clean.)
- [x] T9 Add documentary backdate-miss test. (Done 2026-09-24: `test_backdated_write_stays_missed_documents_residual` in `test_bucketed_incremental_incremental.py` — id 2 backdated below W stays stale, fresh rewrite converges; full incremental module 9 passed. Note: id 1 cannot demonstrate the miss because its v2 row sits at W and is re-selected every run, the documented price of `>=`.)
- [x] T10 Skip detection query when `row_count == 0`; assert zero `writes_detected` on empty build. (Done 2026-09-24: guard `row_count > 0` in macro `L286`, spec empty-maximum clarified, both empty-build tests assert zero detection queries; empty/defaults subset plus concurrency module passed.)
- [x] T11 Floor `rows_per_bucket >= 1`, default 100000, no max-bucket cap. (Done 2026-09-24: default `100000` in macro `L20`, floor explicit in error message `L67-71` and in spec/design/README; unbounded `range(bucket_count)` stays operator responsibility.)
- [x] T12 Partition by the low-cardinality `region` column in the fixture with a cardinality warning in design. (Done 2026-09-24: `bi_partitioned` uses `partition_by='region'`; new `region LowCardinality(String)` demo column rides `bi_model_sql()` through all fixture models; partition test asserts the region key; design config table warns against key partitioning.)
- [x] T13 Note modulo full-scan cost next to `rows_per_bucket` guidance (`schema-pk-filter-on-orderby` tradeoff) so operators size buckets for memory vs total scan cost deliberately. (Done 2026-09-24: README `rows_per_bucket` bullet plus design D9 consequences and config-table pointer state the non-sargable modulo → full scan per bucket tradeoff.)

## 8. References

- Implementation: `macros/materializations/bucketed_incremental.sql`, `macros/materializations/is_incremental.sql`.
- Contract: `docs/bucketed_incremental/spec.md`, `docs/bucketed_incremental/design.md`.
- Hazard analysis: `docs/bucketed_incremental/concurrent-writes-data-loss.md`.
- Test record: `docs/bucketed_incremental/integration-test-plan.md`, `docs/bucketed_incremental/integration-test-implementation.md`.
- Suite: `tests/test_bucketed_incremental_{validation,build,publish,incremental,concurrency}.py`, `tests/test_clickhouse.py`, `tests/helpers.py`, `tests/fixtures/dbt`.
