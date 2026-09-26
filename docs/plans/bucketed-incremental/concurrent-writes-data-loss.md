# Data loss from concurrent writes during bucketed rebuilds

## TL;DR

Each bucket statement sees the source at a different moment: ClickHouse gives a consistent snapshot *per query*, but there is **no snapshot shared across separate statements**, and each bucket is its own statement. Without a guard, a rebuild can miss writes that land while it runs. The materialization now closes that hazard for append-only sources:

- **Snapshot pinning.** `bucket_snapshot_column` is required and must be a non-null `DateTime64(9)`. The count query captures `S0 = max(snapshot)` and every bucket reads only rows up to `S0`. A write landing mid-build carries a stamp at or above `S0`, so it is excluded from all buckets and cannot corrupt the build.
- **Tie-safe watermark contract.** The model must select changed keys with `snapshot >= high_watermark`, where the watermark is the max snapshot in the built target (`W <= S0`). Every mid-build write has stamp `>= S0 >= W`, so the next incremental run re-selects it, including a write stamped exactly `S0`.
- **Fail-closed detection.** After the bucket loop the macro compares `max(snapshot)` with `S0`. `on_concurrent_writes` defaults to `error`: the build is discarded and the target is left untouched. `warn` and `ignore` are explicit opt-outs.

Residual loss, not closable by the macro. How often it occurs depends on where the stamp comes from (see below):

| Case | Effect |
|---|---|
| Row stamped below S0 landing after its bucket — PeerDB: cluster clock skew, clock step backward, manual writes with explicit timestamps (rare per row) | Never re-selected while `W >= S_w`; stale until a later write to the key or the next full refresh |
| Row stamped below S0 landing after its bucket — own updated-at column: late-arriving facts, backfills, out-of-order delivery, source-clock corrections (routine) | Same effect; quiesced rebuilds plus a post-rebuild incremental are required, not optional |
| Physical row delete after its bucket (non-PeerDB sources) | Stale row until the next full refresh; no version exists to re-select it |

## Watermark provenance: destination-stamped vs source-stamped

PeerDB's `_peerdb_synced_at` is stamped by the destination at normalize-insert (`DateTime64(9) DEFAULT now64()` in `vendor/peerdb/flow/connectors/clickhouse/normalize.go`; the normalize `INSERT INTO ... SELECT` never writes the column explicitly). On a single node that clock is monotonic, so a write landing mid-build carries `S_w >= S0` and late-arriving *source* data is not backdated — it gets a fresh stamp on arrival. QRep flows use the same normalize path, so the same holds for them.

An own updated-at column on a non-PeerDB source is stamped upstream, so the macro sees whatever order the source produces. The pinning, `>=` watermark, and detection mechanics are unchanged, but backdates stop being a corner: quiesce the source for rebuilds and always follow with an incremental run.

## What the macro does (by section)

- **Validation**, before any database work and on every run type:
  - `bucket_key_column`, `bucket_snapshot_column` and each element of `bucket_ref` or `bucket_source` must be bare identifiers (letters, digits, underscore); exactly one of the two relation keys must be set.
  - `bucket_snapshot_column` must differ from `bucket_key_column`.
  - `unique_key` must be the single `bucket_key_column`.
  - `rows_per_bucket` must be a positive integer (`true` is rejected).
  - `on_concurrent_writes` must be one of `warn`, `error`, `ignore`.
  - `inserts_only` is rejected, the resolved strategy must be `delete_insert`, and `adapter.validate_incremental_strategy` is called unconditionally.
- **Full refresh**: the resolved bucket relation must exist and be a table. The key column type is inferred from the source: `UUID`, a signed `Int*` or an unsigned `UInt*`; `Nullable`/wrapped types are rejected. The snapshot column must match `DateTime64(9)` with an optional timezone. The count query returns `count()`, the negative-key count for integer keys, and `toString(max(snapshot))` in one statement. Bucket `i` runs `where key % N = i and snapshot <= S0`; the first bucket is the CTAS that creates the intermediate relation, the rest insert into it.
- **Detection** (skip with `ignore`): `select max(snapshot) > S0` — catches only a raised maximum, not truncates or deletes that lower the source without raising it. `error` raises before the publish swap; `warn` logs.
- **Publish**: plain rename on the first run, `EXCHANGE TABLES` when the existing table reports `can_exchange`, two renames otherwise. A failed build leaves the target untouched.

## Why `>=` is required

A write landing mid-build carries `S_w >= S0` (monotonic destination clock; PeerDB's `_peerdb_synced_at` is `DateTime64(9) DEFAULT now64()` in `vendor/peerdb/flow/connectors/clickhouse/normalize.go`). The built target's maximum is `W <= S0`. The next incremental run re-selects a key iff its stamp is `>= W`:

- `S_w > S0` is always recovered.
- `S_w == S0` (a clock-resolution tie) is recovered by `>= W` because `W <= S0`; with a strict `>` it can be missed forever. `now64()` defaults to millisecond resolution, so ties are reachable and the contract is deliberate.
- A backdated stamp `S_w < S0` is not recovered: `W` can sit at or above `S_w`, and no later run re-selects the key unless the key is written again.

Recovery assumes a monotonic watermark column. `_peerdb_synced_at` is stamped by the destination at normalize-insert, which holds on a single node; across shards or replicas, clock skew produces the backdated residual case. Own updated-at columns carry no such guarantee: late, backfilled, or re-stated source rows routinely arrive with old stamps, so the backdated case is their normal operating condition rather than a corner.

## Worked example

Source `src.events`, 3,000,000 rows, e.g. `rows_per_bucket = 1000000` → `N = 3`. Buckets run in order with predicates `id % 3 = 0`, `= 1`, `= 2`, each with `_peerdb_synced_at <= S0`.

| Time | Event |
|---|---|
| 10:00:00 | Count query sees 3,000,000 rows and `S0 = 10:00:00`; `N = 3`. |
| 10:00:01–10:15 | Bucket 0 builds. It captures `id = 99` v1 (synced 09:55). |
| 10:15:01–10:35 | Bucket 1 builds. |
| 10:36:00 | PeerDB applies `id = 99` v2 (synced 10:36). Bucket 0 is done, and the bound excludes v2 from every other bucket. |
| 10:40:01–11:00 | Bucket 2 builds. |
| 11:00:01 | Publish. The target's max is `W = S0 = 10:00:00` (the row that set S0 was captured). |
| Next run | `changed_keys` uses `_peerdb_synced_at >= 10:00:00`, so `id = 99` is re-selected, all its versions re-read, and v2 delete+inserted: full recovery with no operator action. |

Tie variant: had v2 landed at exactly 10:00:00 after bucket 0 was read, the bound would still exclude it from the build, and `>= W` with `W = 10:00:00` would still re-select it.

Backdated variant: a write stamped 09:50 landing after its bucket is not re-selected (`09:50 >= W` is false), so the target keeps the built value until the key is written again or the next full refresh.

Delete variant: PeerDB CDC never hard-deletes normalized rows; a delete arrives as a tombstone version (`_peerdb_is_deleted = 1`). A missed tombstone is re-selected and applied by the next incremental run. For sources that physically delete rows (non-PeerDB tables, manual deletes, retention jobs), a missed delete leaves a stale row that no incremental run clears: quiesce the source for rebuilds.

## Environment notes

- The bucketed-build hazard does not depend on version or topology: `enable_shared_storage_snapshot_in_query` shares a snapshot only *within* one query, and each bucket is a separate query.
- A single-node deployment keeps `_peerdb_synced_at` on one clock and removes replica lag, so the ordering assumed above holds. This is a property of destination-stamped PeerDB columns, not of the macro: own updated-at columns bring their own clock discipline.
- On 25.12+ the setting defaults on, so the incremental path's two source scans (`changed_keys`, `raw_versions`) share one snapshot; before that, a version landing between the scans can stay missed (inherited from upstream, not introduced here).

## Decision record (2026-09-20)

- Implement **C** (snapshot pinning) and **B** (detection). **A** and **D** remain the fallback for physical deletes.
- Strict profile: `bucket_snapshot_column` required and restricted to non-null `DateTime64(9)`; the key type is inferred from the source column; `unique_key` must equal `bucket_key_column`; strategy and `inserts_only` validation on every run; `on_concurrent_writes` defaults to `error`.
- The `>=` watermark is a documented model contract because the macro cannot inspect the model's watermark predicate.
