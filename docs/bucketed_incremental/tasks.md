# bucketed_incremental Implementation Plan

> **Status: superseded.** This plan rebuilt the materialization and specified a
> Docker-based harness in `integration_tests/`. The implemented materialization
> and the current suite (fixture project `tests/fixtures/dbt`, local ClickHouse
> via `clickhousectl`) are documented in `integration-test-plan.md` and
> `integration-test-implementation.md`. The task list below is kept for history;
> do not follow it.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the `bucketed_incremental` ClickHouse materialization from the spec, with a Docker-backed integration test suite that proves every requirement.

**Architecture:** One dbt materialization with two paths: a bucketed full refresh (count, sequential key buckets into an intermediate relation, concurrent-write detection, atomic publish) and an adapter delete+insert incremental path. Tests run a dbt project in `integration_tests/` against a ClickHouse server in Docker and assert table contents, run logs and `system.query_log` entries.

**Tech Stack:** dbt-core 1.10/1.11, dbt-clickhouse 1.10.2, ClickHouse 25.12 in Docker, pytest, clickhouse-connect, uv, GNU Make.

**Spec:** `docs/bucketed_incremental/spec.md`
**Design:** `docs/bucketed_incremental/design.md`

**Starting point:** Work on a feature branch. Delete `macros/materializations/bucketed_incremental.sql` before Task 1; git history and `docs/bucketed_incremental/design.md` hold the reference, and this plan rebuilds it. Verify that `macros/materializations/is_incremental.sql` lists `bucketed_incremental` (Task 2).

## Global Constraints

- `dbt_project.yml` keeps `require-dbt-version: [">=1.3.0", "<1.12.0"]`; the test environment pins `dbt-clickhouse==1.10.2`.
- Every config name, enum value and error string is fixed. Error messages carry the prefix `bucketed_incremental:` and match the table in `docs/bucketed_incremental/design.md`.
- The test server runs with query logging on; the dbt profile sets `use_lw_deletes: true`. The adapter enables `allow_nondeterministic_mutations` for its session when the user may set it.
- The materialization supports the `delete_insert` strategy only and rejects `inserts_only`.
- Tests use the ClickHouse database `test` and drop it at the start of every test that seeds data.
- American English in messages and docs.
- Jinja style follows the existing macros: two-space indentation and `{%- ... -%}` trimming on config lines.

## Review Focus

- Physical row deletes: the macro cannot recover a missed delete. Tests cover the append-only contract only.
- Backdated writes: a stamp below `S0` landing after its bucket is not re-selected. The detection test writes with a current timestamp only.
- Clock resolution: the tie test inserts a row stamped exactly at `S0`. A production tie is rarer and depends on `now64()` resolution.
- Empty source with `on_concurrent_writes="error"`: detection must not fail a legitimate empty build.
- Engines without atomic exchange: the two-rename fallback is untested; the Docker database engine supports `EXCHANGE TABLES`.
- A model whose full-history branch returns duplicate keys: the target keeps duplicates. Tests pin the dedupe contract, not enforcement.

## File Structure

| File | Responsibility |
|------|----------------|
| `docker-compose.yml` | ClickHouse test server |
| `integration_tests/dbt_project.yml` | Test project |
| `integration_tests/profiles.yml` | Test target with `use_lw_deletes: true` |
| `integration_tests/packages.yml` | Includes this package by local path |
| `integration_tests/models/sources.yml` | Source table declarations |
| `integration_tests/models/bucketed.sql` | Parametrized model under test |
| `integration_tests/models/bucketed_slow.sql` | Slow model for the concurrency test |
| `integration_tests/models/harness_view.sql` | Harness smoke model |
| `integration_tests/tests/conftest.py` | ClickHouse client, dbt runner, seeds, background writer |
| `integration_tests/tests/test_*.py` | Requirement tests |
| `macros/materializations/bucketed_incremental.sql` | The materialization |
| `macros/materializations/is_incremental.sql` | Incremental detection |
| `Makefile` | `test`, `test-up`, `test-down` targets |
| `pyproject.toml` | Dev dependencies |

---

### Task 1: Integration Harness

**Files:**
- Create: `docker-compose.yml`
- Create: `integration_tests/dbt_project.yml`
- Create: `integration_tests/profiles.yml`
- Create: `integration_tests/packages.yml`
- Create: `integration_tests/models/sources.yml`
- Create: `integration_tests/models/harness_view.sql`
- Create: `integration_tests/tests/conftest.py`
- Create: `integration_tests/tests/test_harness.py`
- Create: `integration_tests/__init__.py` (empty)
- Create: `integration_tests/tests/__init__.py` (empty)
- Modify: `Makefile`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: `client` fixture (a `clickhouse_connect` client connected to host `localhost`, port `18123`, database `test`); `dbt(*args, vars=None)` runner fixture returning `subprocess.CompletedProcess`; `sources` fixture that recreates database `test` and all source tables and yields the client; `seed_responses(sources)` fixture returning the seeded rows; `assert_ok(result)`, `assert_failed(result, message)`; `flush_logs(client)`; `query_log(client, pattern, since=None)`; `BackgroundWriter(client, table, key, wait_for)` that waits for a running bucket query before writing; constants `SCHEMA`, `COLUMNS`, `T1`, `T2`, `T3`, `U1`-`U5`.

- [ ] **Step 1: Write the failing harness test**

Create `integration_tests/tests/test_harness.py`:

```python
from integration_tests.tests.conftest import SCHEMA, assert_ok


def test_clickhouse_reachable(client):
    assert client.query("select 1").result_rows == [(1,)]


def test_dbt_project_builds_view(sources, dbt):
    result = dbt("run", "--select", "harness_view", "--full-refresh")
    assert_ok(result)
    rows = sources.query(f"select id from {SCHEMA}.harness_view").result_rows
    assert rows == [(1,)]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest integration_tests/tests/test_harness.py -v`

Expected: collection errors because `integration_tests/tests/conftest.py` and the dbt project do not exist.

- [ ] **Step 3: Create the ClickHouse server compose file**

Create `docker-compose.yml`:

```yaml
services:
  clickhouse:
    image: clickhouse/clickhouse-server:25.12
    ports:
      - "18123:8123"
      - "19000:9000"
    environment:
      CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: "1"
    ulimits:
      nofile:
        soft: 262144
        hard: 262144
    healthcheck:
      test: ["CMD-SHELL", "clickhouse-client --query 'SELECT 1'"]
      interval: 2s
      timeout: 2s
      retries: 60
```

- [ ] **Step 4: Create the dbt test project**

Create `integration_tests/dbt_project.yml`:

```yaml
name: dbt_peerdb_integration_tests
version: 1.0.0
config-version: 2

profile: dbt_peerdb

model-paths: ["models"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
test-paths: ["tests"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

vars:
  dbt_peerdb_columns: [_peerdb_synced_at, _peerdb_is_deleted, _peerdb_version]

models:
  dbt_peerdb_integration_tests:
    +materialized: view
```

Create `integration_tests/profiles.yml`:

```yaml
dbt_peerdb:
  target: clickhouse
  outputs:
    clickhouse:
      type: clickhouse
      driver: http
      host: "{{ env_var('CLICKHOUSE_HOST', 'localhost') }}"
      port: "{{ env_var('CLICKHOUSE_PORT', '18123') | int }}"
      user: "{{ env_var('CLICKHOUSE_USER', 'default') }}"
      password: "{{ env_var('CLICKHOUSE_PASSWORD', '') }}"
      schema: "{{ env_var('CLICKHOUSE_SCHEMA', 'test') }}"
      secure: false
      threads: 1
      use_lw_deletes: true
```

Create `integration_tests/packages.yml`:

```yaml
packages:
  - local: ..
```

Create `integration_tests/models/sources.yml`:

```yaml
version: 2

sources:
  - name: test
    database: "{{ env_var('CLICKHOUSE_SCHEMA', 'test') }}"
    tables:
      - name: raw_responses
      - name: raw_responses_empty
      - name: raw_responses_slow
      - name: raw_responses_int
      - name: raw_responses_uint
      - name: raw_responses_negative
      - name: raw_responses_bad_snapshot
      - name: raw_responses_text
```

Create `integration_tests/models/harness_view.sql`:

```sql
select 1 as id
```

- [ ] **Step 5: Create the pytest fixtures**

Create `integration_tests/tests/conftest.py`:

```python
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect
import pytest
from clickhouse_connect.driver.client import Client

PROJECT_DIR = Path(__file__).parent.parent
SCHEMA = os.environ.get("CLICKHOUSE_SCHEMA", "test")
DBT = shutil.which("dbt")

HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
PORT = int(os.environ.get("CLICKHOUSE_PORT", "18123"))
USER = os.environ.get("CLICKHOUSE_USER", "default")
PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")

COLUMNS = ["key", "payload", "_peerdb_synced_at", "_peerdb_version", "_peerdb_is_deleted"]

T1 = "2026-01-01 00:00:00.000000000"
T2 = "2026-01-02 00:00:00.000000000"
T3 = "2026-01-03 00:00:00.000000000"

U1 = "00000000-0000-0000-0000-000000000001"
U2 = "00000000-0000-0000-0000-000000000002"
U3 = "00000000-0000-0000-0000-000000000003"
U4 = "00000000-0000-0000-0000-000000000004"
U5 = "00000000-0000-0000-0000-000000000005"


def pytest_configure():
    if DBT is None:
        raise RuntimeError("dbt executable not found on PATH; run tests with `uv run pytest`")


def assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def assert_failed(result: subprocess.CompletedProcess, message: str) -> None:
    assert result.returncode != 0, result.stdout + result.stderr
    assert message in result.stdout + result.stderr, result.stdout + result.stderr


def flush_logs(client: Client) -> None:
    client.command("system flush logs")


def query_log(client: Client, pattern: str, since: datetime | None = None) -> list[str]:
    filters = ["type = 'QueryFinish'", f"query like '%{pattern}%'"]
    if since is not None:
        filters.append(f"event_time >= toDateTime('{since:%Y-%m-%d %H:%M:%S}')")
    result = client.query("select query from system.query_log where " + " and ".join(filters))
    return [row[0] for row in result.result_rows]


def create_source(
    client: Client, name: str, key_type: str = "UUID", snapshot_type: str = "DateTime64(9)"
) -> None:
    client.command(
        f"""
        create table {SCHEMA}.{name} (
            key {key_type},
            payload String,
            _peerdb_synced_at {snapshot_type},
            _peerdb_version UInt64,
            _peerdb_is_deleted UInt8
        )
        engine = MergeTree()
        order by key
        """
    )


@pytest.fixture(scope="session")
def client() -> Iterator[Client]:
    client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD)
    client.command(f"create database if not exists {SCHEMA}")
    yield client
    client.close()


@pytest.fixture(scope="session", autouse=True)
def dbt_deps() -> None:
    subprocess.run(
        [DBT, "deps", "--project-dir", str(PROJECT_DIR), "--profiles-dir", str(PROJECT_DIR)],
        cwd=PROJECT_DIR,
        check=True,
    )


@pytest.fixture
def dbt() -> Callable[..., subprocess.CompletedProcess]:
    def run(*args: str, vars: dict[str, Any] | None = None) -> subprocess.CompletedProcess:
        command = [
            DBT,
            *args,
            "--project-dir",
            str(PROJECT_DIR),
            "--profiles-dir",
            str(PROJECT_DIR),
            "--target",
            "clickhouse",
        ]
        if vars is not None:
            command += ["--vars", json.dumps(vars)]
        return subprocess.run(command, cwd=PROJECT_DIR, capture_output=True, text=True)

    return run


@pytest.fixture
def sources(client: Client) -> Iterator[Client]:
    client.command(f"drop database if exists {SCHEMA}")
    client.command(f"create database {SCHEMA}")
    for name in ("raw_responses", "raw_responses_empty", "raw_responses_slow"):
        create_source(client, name)
    create_source(client, "raw_responses_int", key_type="Int64")
    create_source(client, "raw_responses_uint", key_type="UInt64")
    create_source(client, "raw_responses_negative", key_type="Int64")
    create_source(client, "raw_responses_bad_snapshot", snapshot_type="String")
    create_source(client, "raw_responses_text", key_type="String")
    yield client


@pytest.fixture
def seed_responses(sources: Client) -> list[tuple]:
    rows = [
        (U1, "a1", T1, 1, 0),
        (U1, "a2", T2, 2, 0),
        (U2, "b1", T1, 1, 0),
        (U3, "c1", T1, 1, 0),
        (U4, "d1", T1, 1, 0),
        (U5, "e1", T1, 1, 0),
    ]
    sources.insert(f"{SCHEMA}.raw_responses", rows, column_names=COLUMNS)
    return rows


class BackgroundWriter:
    """Waits for a running bucket query, then inserts a fresh row every 50ms until stopped."""

    def __init__(self, client: Client, table: str, key: str, wait_for: str) -> None:
        self.client = client
        self.table = table
        self.key = key
        self.wait_for = wait_for
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _bucket_running(self) -> bool:
        result = self.client.query(
            "select count() from system.processes where position(query, %(pattern)s) > 0",
            parameters={"pattern": self.wait_for},
        )
        return result.result_rows[0][0] > 0

    def _run(self) -> None:
        while not self._stop.wait(0.02):
            if not self._bucket_running():
                continue
            self.client.insert(
                f"{SCHEMA}.{self.table}",
                [(self.key, "late", datetime.now(timezone.utc).replace(tzinfo=None), 99, 0)],
                column_names=COLUMNS,
            )

    def __enter__(self) -> "BackgroundWriter":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        self._thread.join()
```

Create empty `integration_tests/__init__.py` and `integration_tests/tests/__init__.py`.

- [ ] **Step 6: Add the Makefile targets and dev dependencies**

Append to `Makefile`:

```makefile
# ==============================================================================
# TEST
# ==============================================================================

test: test-up
	uv run pytest integration_tests/tests -v

test-up:
	docker compose up -d --wait

test-down:
	docker compose down -v
```

Add to `pyproject.toml`:

```toml
[dependency-groups]
dev = [
  "clickhouse-connect>=0.8",
  "dbt-clickhouse==1.10.2",
  "pytest>=8",
]
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `make test`

Expected: `test_harness.py` passes, 2 passed. The first run pulls the ClickHouse image and installs dev dependencies.

- [ ] **Step 8: Commit**

```bash
git add docker-compose.yml integration_tests pyproject.toml Makefile
git commit -m "test: add ClickHouse integration harness"
```

---

### Task 2: Materialization Skeleton and First Bucketed Build

**Files:**
- Create: `macros/materializations/bucketed_incremental.sql`
- Modify: `macros/materializations/is_incremental.sql`
- Create: `integration_tests/models/bucketed.sql`
- Create: `integration_tests/tests/test_full_refresh.py`

**Interfaces:**
- Consumes: the `client`, `dbt`, `sources` and `seed_responses` fixtures from Task 1.
- Produces: the `bucketed_incremental` materialization; the `bucketed` model whose config values come from `--vars` with defaults `bucket_key_column="key"`, `bucket_source_table="<schema>.raw_responses"`, `bucket_snapshot_column="_peerdb_synced_at"`, `rows_per_bucket=1000000`, `on_concurrent_writes="error"`, `incremental_strategy="delete_insert"`, `unique_key="key"`, `source_name="raw_responses"`; a `path` column with values `full` on a full refresh and `incr` on an incremental run; helper `rows_by_key(sources, table="bucketed")` in the test module.

- [ ] **Step 1: Write the failing test**

Create `integration_tests/tests/test_full_refresh.py`:

```python
from integration_tests.tests.conftest import (
    SCHEMA,
    U1,
    U2,
    U3,
    U4,
    U5,
    assert_ok,
)


def rows_by_key(sources, table="bucketed"):
    rows = sources.query(f"select toString(key), payload, path from {SCHEMA}.{table}").result_rows
    return {row[0]: (row[1], row[2]) for row in rows}


def test_first_run_builds_one_row_per_key(sources, seed_responses, dbt):
    result = dbt("run", "--select", "bucketed")
    assert_ok(result)

    assert rows_by_key(sources) == {
        U1: ("a2", "full"),
        U2: ("b1", "full"),
        U3: ("c1", "full"),
        U4: ("d1", "full"),
        U5: ("e1", "full"),
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest integration_tests/tests/test_full_refresh.py -v`

Expected: FAIL with `Materialization 'bucketed_incremental' not found` or a missing-model compilation error.

- [ ] **Step 3: Create the materialization**

Create `macros/materializations/bucketed_incremental.sql`:

```jinja
{% materialization bucketed_incremental, adapter='clickhouse' %}

  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='table') -%}

  {%- set unique_key = config.get('unique_key') -%}
  {% if unique_key is not none and unique_key|length == 0 %}
    {% set unique_key = none %}
  {% endif %}
  {% if unique_key is iterable and (unique_key is not string and unique_key is not mapping) %}
     {% set unique_key = unique_key|join(', ') %}
  {% endif %}
  {%- set inserts_only = config.get('inserts_only') -%}
  {%- set grant_config = config.get('grants') -%}
  {%- set has_contract = config.get('contract').enforced -%}
  {%- set full_refresh_mode = (should_full_refresh() or existing_relation is none or existing_relation.is_view) -%}
  {%- set on_schema_change = incremental_validate_on_schema_change(config.get('on_schema_change'), default='ignore') -%}
  {%- set bucket_key_column = config.get('bucket_key_column', none) -%}
  {%- set rows_per_bucket = config.get('rows_per_bucket', 1000000) -%}
  {%- set bucket_source_table = config.get('bucket_source_table', none) -%}
  {%- set bucket_snapshot_column = config.get('bucket_snapshot_column', none) -%}
  {%- set on_concurrent_writes = config.get('on_concurrent_writes', 'error') -%}
  {%- set marker = '-- __BUCKET_PREDICATE__' -%}

  {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation) -%}

  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% if full_refresh_mode %}
    {% set bucket_lhs = 'reinterpretAsUInt64(' ~ bucket_key_column ~ ')' %}
    {% set build_relation = intermediate_relation %}
    {% set bucket_count = 1 %}
    {% for i in range(bucket_count) %}
      {% set predicate = 'where ' ~ bucket_lhs ~ ' % ' ~ bucket_count ~ ' = ' ~ i %}
      {% set bucket_sql = sql.replace(marker, predicate) %}
      {{ log(
          'bucketed_incremental: Processing bucket ' ~ (i + 1) ~ ' of ' ~ bucket_count, info=True
      ) }}
      {% if loop.first %}
        {% call statement('main') %}
          {{ get_create_table_as_sql(False, build_relation, bucket_sql) }}
        {% endcall %}
      {% else %}
        {% call statement('bucket_' ~ i) %}
          {{ clickhouse__insert_into(build_relation, bucket_sql, has_contract) }}
        {% endcall %}
      {% endif %}
    {% endfor %}
    {% do adapter.rename_relation(intermediate_relation, target_relation) %}
  {% else %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: incremental runs are not implemented yet.'
    ) }}
  {% endif %}

  {% do persist_docs(target_relation, model) %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {% do adapter.commit() %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target_relation]}) }}

{%- endmaterialization %}
```

- [ ] **Step 4: Verify incremental detection**

Open `macros/materializations/is_incremental.sql`. Line 10 must be:

```jinja
                  and (model.config.materialized == 'incremental' or model.config.materialized == 'distributed_incremental' or model.config.materialized == 'bucketed_incremental')
```

If it is not, replace the line with the version above.

- [ ] **Step 5: Create the parametrized model**

Create `integration_tests/models/bucketed.sql`:

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy=var("incremental_strategy", "delete_insert"),
    engine=var("engine", "MergeTree()"),
    unique_key=var("unique_key", "key"),
    order_by=var("order_by", "key"),
    bucket_key_column=var("bucket_key_column", "key"),
    bucket_source_table=var("bucket_source_table", env_var("CLICKHOUSE_SCHEMA", "test") ~ ".raw_responses"),
    bucket_snapshot_column=var("bucket_snapshot_column", "_peerdb_synced_at"),
    rows_per_bucket=var("rows_per_bucket", 1000000),
    on_concurrent_writes=var("on_concurrent_writes", "error"),
    inserts_only=var("inserts_only", false),
    on_schema_change=var("on_schema_change", "ignore")
) }}

{% set key = var("bucket_key_column", "key") %}
{% set source_name = var("source_name", "raw_responses") %}

with raw_versions as (
    select
        {{ key }} as key,
        payload,
        _peerdb_synced_at
    from {{ source("test", source_name) }}
    {% if is_incremental() %}
    where {{ key }} in (
        select distinct {{ key }}
        from {{ source("test", source_name) }}
        where _peerdb_synced_at >= (
            select coalesce(
                max(_peerdb_synced_at),
                toDateTime64('1970-01-01 00:00:00.000000000', 9)
            )
            from {{ this }}
        )
    )
    {% else %}
    -- __BUCKET_PREDICATE__
    {% endif %}
),

deduped as (
    select
        key,
        payload,
        _peerdb_synced_at
    from raw_versions
    order by _peerdb_synced_at desc
    limit 1 by key
)

select
    key,
    payload,
    _peerdb_synced_at,
    {% if is_incremental() %}'incr'{% else %}'full'{% endif %} as path
from deduped
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest integration_tests/tests/test_full_refresh.py -v`

Expected: 1 passed. The log contains `Processing bucket 1 of 1`.

- [ ] **Step 7: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql macros/materializations/is_incremental.sql integration_tests/models/bucketed.sql integration_tests/tests/test_full_refresh.py
git commit -m "feat: build bucketed_incremental full refresh into an intermediate relation"
```

---

### Task 3: Bucket Sizing

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Modify: `integration_tests/tests/test_full_refresh.py`

**Interfaces:**
- Consumes: Task 2's materialization and model.
- Produces: bucket count `ceil(row_count / rows_per_bucket)`; run log line `bucketed_incremental: row_count=<r> bucket_count=<n> snapshot_max=<value>` (the `snapshot_max` part arrives in Task 7); per-bucket log lines `bucketed_incremental: Processing bucket <i> of <n>`.

- [ ] **Step 1: Write the failing test**

Append to `integration_tests/tests/test_full_refresh.py`:

```python
def test_bucket_count_uses_rows_per_bucket(sources, seed_responses, dbt):
    result = dbt("run", "--select", "bucketed", vars={"rows_per_bucket": 2})
    assert_ok(result)

    assert "bucketed_incremental: row_count=6 bucket_count=3" in result.stdout
    assert "bucketed_incremental: Processing bucket 1 of 3" in result.stdout
    assert "bucketed_incremental: Processing bucket 3 of 3" in result.stdout

    assert len(rows_by_key(sources)) == 5


def test_single_bucket_by_default(sources, seed_responses, dbt):
    result = dbt("run", "--select", "bucketed")
    assert_ok(result)

    assert "bucketed_incremental: row_count=6 bucket_count=1" in result.stdout
    assert "bucketed_incremental: Processing bucket 1 of 1" in result.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_full_refresh.py -v`

Expected: the two new tests FAIL with `row_count` missing from the log.

- [ ] **Step 3: Implement the count query and the bucket formula**

In `macros/materializations/bucketed_incremental.sql`, replace:

```jinja
    {% set build_relation = intermediate_relation %}
    {% set bucket_count = 1 %}
    {% for i in range(bucket_count) %}
```

with:

```jinja
    {% set build_relation = intermediate_relation %}
    {% set count_sql %}select count() as row_count from {{ bucket_source_table }}{% endset %}
    {% set count_result = run_query(count_sql) %}
    {% set row_count = count_result.columns[0].values()[0] | int %}
    {% set bucket_count = ((row_count / rows_per_bucket) | round(0, 'ceil')) | int %}
    {{ log(
        'bucketed_incremental: row_count=' ~ row_count ~ ' bucket_count=' ~ bucket_count, info=True
    ) }}
    {% for i in range(bucket_count) %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_full_refresh.py -v`

Expected: 3 passed. With `rows_per_bucket=2`, three buckets each build a `key % 3 = i` predicate; the target still holds 5 deduped rows.

- [ ] **Step 5: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/tests/test_full_refresh.py
git commit -m "feat: size buckets from the source row count"
```

---

### Task 4: Marker Contract and Empty Source

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Create: `integration_tests/models/no_marker.sql`
- Create: `integration_tests/models/two_markers.sql`
- Create: `integration_tests/tests/test_marker.py`

**Interfaces:**
- Consumes: Task 3's materialization.
- Produces: a marker check that stops the run when the marker count is not one; an empty-source build using `where 1 = 0`.

- [ ] **Step 1: Write the failing tests**

Create `integration_tests/tests/test_marker.py`:

```python
from integration_tests.tests.conftest import SCHEMA, assert_failed, assert_ok


def test_missing_marker_aborts(sources, seed_responses, dbt):
    result = dbt("run", "--select", "no_marker")
    assert_failed(
        result, "marker -- __BUCKET_PREDICATE__ must appear exactly once in the model SQL; found 0"
    )


def test_duplicated_marker_aborts(sources, seed_responses, dbt):
    result = dbt("run", "--select", "two_markers")
    assert_failed(
        result, "marker -- __BUCKET_PREDICATE__ must appear exactly once in the model SQL; found 2"
    )


def test_empty_source_builds_empty_table(sources, dbt):
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={
            "source_name": "raw_responses_empty",
            "bucket_source_table": f"{SCHEMA}.raw_responses_empty",
        },
    )
    assert_ok(result)

    assert "bucketed_incremental: row_count=0 bucket_count=0" in result.stdout
    assert sources.query(f"select count() from {SCHEMA}.bucketed").result_rows == [(0,)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_marker.py -v`

Expected: `no_marker` and `two_markers` fail with missing models; the empty-source test fails because zero buckets leave no target relation.

- [ ] **Step 3: Add the two models**

Create `integration_tests/models/no_marker.sql`:

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy="delete_insert",
    unique_key="key",
    order_by="key",
    bucket_key_column="key",
    bucket_source_table=env_var("CLICKHOUSE_SCHEMA", "test") ~ ".raw_responses",
    bucket_snapshot_column="_peerdb_synced_at"
) }}

select key, payload, _peerdb_synced_at
from {{ source("test", "raw_responses") }}
where 1 = 1
```

Create `integration_tests/models/two_markers.sql`:

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy="delete_insert",
    unique_key="key",
    order_by="key",
    bucket_key_column="key",
    bucket_source_table=env_var("CLICKHOUSE_SCHEMA", "test") ~ ".raw_responses",
    bucket_snapshot_column="_peerdb_synced_at"
) }}

select key, payload, _peerdb_synced_at
from {{ source("test", "raw_responses") }}
-- __BUCKET_PREDICATE__
where 1 = 1
-- __BUCKET_PREDICATE__
```

- [ ] **Step 4: Add the marker check and the empty build**

In `macros/materializations/bucketed_incremental.sql`, insert after `{% if full_refresh_mode %}`:

```jinja
    {% set marker_count = sql.count(marker) %}
    {% if marker_count != 1 %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: marker ' ~ marker ~ ' must appear exactly once in the model SQL; found '
          ~ marker_count ~ '.'
      ) }}
    {% endif %}
```

Then replace:

```jinja
    {% for i in range(bucket_count) %}
```

with:

```jinja
    {% if bucket_count < 1 %}
      {% set empty_sql = sql.replace(marker, 'where 1 = 0') %}
      {% call statement('main') %}
        {{ get_create_table_as_sql(False, build_relation, empty_sql) }}
      {% endcall %}
    {% else %}
      {% for i in range(bucket_count) %}
```

and replace the loop's closing lines:

```jinja
    {% endfor %}
    {% do adapter.rename_relation(intermediate_relation, target_relation) %}
```

with:

```jinja
      {% endfor %}
    {% endif %}
    {% do adapter.rename_relation(intermediate_relation, target_relation) %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_marker.py -v`

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/models/no_marker.sql integration_tests/models/two_markers.sql integration_tests/tests/test_marker.py
git commit -m "feat: enforce the bucket predicate marker and build empty sources"
```

---

### Task 5: Configuration Validation

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Create: `integration_tests/tests/test_validation.py`

**Interfaces:**
- Consumes: Task 4's materialization and the `bucketed` model.
- Produces: the config checks that run before any relation handle is created, covering `bucket_key_column`, `bucket_snapshot_column`, `bucket_source_table`, `unique_key`, `rows_per_bucket`, `on_concurrent_writes`, `inserts_only` and the resolved strategy.

- [ ] **Step 1: Write the failing tests**

Create `integration_tests/tests/test_validation.py`:

```python
import pytest

from integration_tests.tests.conftest import assert_failed

CASES = [
    (
        {"bucket_key_column": None},
        "bucket_key_column is required and must be a bare column identifier",
    ),
    (
        {"bucket_key_column": "bad-name"},
        "bucket_key_column is required and must be a bare column identifier",
    ),
    (
        {"bucket_snapshot_column": None},
        "bucket_snapshot_column is required and must be a bare column identifier",
    ),
    (
        {"bucket_snapshot_column": "key"},
        "bucket_snapshot_column must be a different column from bucket_key_column",
    ),
    ({"bucket_source_table": None}, "bucket_source_table is required and must have the form"),
    (
        {"bucket_source_table": "raw_responses"},
        "bucket_source_table is required and must have the form",
    ),
    ({"unique_key": "other"}, "unique_key must be the single bucket_key_column"),
    ({"rows_per_bucket": 0}, "rows_per_bucket must be a positive integer"),
    ({"rows_per_bucket": True}, "rows_per_bucket must be a positive integer"),
    ({"rows_per_bucket": 1.5}, "rows_per_bucket must be a positive integer"),
    (
        {"on_concurrent_writes": "raise"},
        'on_concurrent_writes must be one of "warn", "error", "ignore"',
    ),
    ({"inserts_only": True}, "inserts_only is not supported"),
    (
        {"incremental_strategy": "append"},
        "only the delete_insert incremental strategy is supported",
    ),
    (
        {"incremental_strategy": "legacy"},
        "only the delete_insert incremental strategy is supported",
    ),
]


@pytest.mark.parametrize(("variables", "message"), CASES)
def test_config_validation(sources, seed_responses, dbt, variables, message):
    result = dbt("run", "--select", "bucketed", vars=variables)
    assert_failed(result, message)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_validation.py -v`

Expected: every case FAILS because the run does not stop, or stops with a different message (for example a database error from the null identifier).

- [ ] **Step 3: Add the validation block**

In `macros/materializations/bucketed_incremental.sql`, insert after `{%- set marker = '-- __BUCKET_PREDICATE__' -%}` and before `{%- set intermediate_relation = make_intermediate_relation(target_relation) -%}`:

```jinja
  {%- set identifier_pattern = '^[A-Za-z_][A-Za-z0-9_]*$' -%}
  {%- set source_pattern = '^[A-Za-z_][A-Za-z0-9_]*[.][A-Za-z_][A-Za-z0-9_]*$' -%}

  {% if bucket_key_column is none or bucket_key_column is not string or not modules.re.match(identifier_pattern, bucket_key_column|trim) %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_key_column is required and must be a bare column identifier '
        ~ '(letters, digits, underscore), got "' ~ bucket_key_column ~ '".'
    ) }}
  {% endif %}
  {% set bucket_key_column = bucket_key_column|trim %}
  {% if bucket_snapshot_column is none or bucket_snapshot_column is not string or not modules.re.match(identifier_pattern, bucket_snapshot_column|trim) %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_snapshot_column is required and must be a bare column identifier '
        ~ '(letters, digits, underscore), got "' ~ bucket_snapshot_column ~ '".'
    ) }}
  {% endif %}
  {% set bucket_snapshot_column = bucket_snapshot_column|trim %}
  {% if bucket_snapshot_column == bucket_key_column %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_snapshot_column must be a different column from bucket_key_column.'
    ) }}
  {% endif %}
  {% if bucket_source_table is none or bucket_source_table is not string or not modules.re.match(source_pattern, bucket_source_table|trim) %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: bucket_source_table is required and must have the form "database.table" '
        ~ 'with bare identifiers (letters, digits, underscore), got "' ~ bucket_source_table ~ '".'
    ) }}
  {% endif %}
  {% set bucket_source_table = bucket_source_table|trim %}
  {% if unique_key is none or unique_key != bucket_key_column %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: unique_key must be the single bucket_key_column "'
        ~ bucket_key_column ~ '", otherwise versions of one row split across buckets.'
    ) }}
  {% endif %}
  {% if rows_per_bucket is boolean or rows_per_bucket is not number or rows_per_bucket | int != rows_per_bucket or rows_per_bucket < 1 %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: rows_per_bucket must be a positive integer.'
    ) }}
  {% endif %}
  {% if on_concurrent_writes not in ('warn', 'error', 'ignore') %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: on_concurrent_writes must be one of "warn", "error", "ignore", got "'
        ~ on_concurrent_writes ~ '".'
    ) }}
  {% endif %}
  {% if inserts_only %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: inserts_only is not supported; incremental runs always use delete+insert.'
    ) }}
  {% endif %}
  {% set incremental_strategy = adapter.calculate_incremental_strategy(config.get('incremental_strategy')) %}
  {% if incremental_strategy != 'delete_insert' %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: only the delete_insert incremental strategy is supported, got "'
        ~ incremental_strategy ~ '". Set incremental_strategy="delete_insert"; it requires '
        ~ 'use_lw_deletes: true in the profile and a dbt user allowed to set '
        ~ 'allow_nondeterministic_mutations.'
    ) }}
  {% endif %}
  {% set incremental_predicates = config.get('predicates', []) or config.get('incremental_predicates', []) %}
  {% set partition_by = config.get('partition_by') %}
  {% do adapter.validate_incremental_strategy(incremental_strategy, incremental_predicates, unique_key, partition_by) %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_validation.py -v`

Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/tests/test_validation.py
git commit -m "feat: validate bucketed_incremental configuration before any database work"
```

---

### Task 6: Key Types and Source Validation

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Create: `integration_tests/tests/test_keys.py`

**Interfaces:**
- Consumes: Task 5's validation block and the `sources` fixture's tables `raw_responses_int`, `raw_responses_uint`, `raw_responses_negative`.
- Produces: source relation resolution, table check, column lookup, key dtype checks, and the negative integer key check.

- [ ] **Step 1: Write the failing tests**

Create `integration_tests/tests/test_keys.py`:

```python
import pytest

from integration_tests.tests.conftest import (
    COLUMNS,
    SCHEMA,
    T1,
    T3,
    assert_failed,
    assert_ok,
    flush_logs,
    query_log,
)


def seed_int(sources, table="raw_responses_int"):
    rows = [
        (1, "a1", T1, 1, 0),
        (1, "a2", T3, 2, 0),
        (2, "b1", T1, 1, 0),
        (3, "c1", T1, 1, 0),
    ]
    sources.insert(f"{SCHEMA}.{table}", rows, column_names=COLUMNS)
    return rows


def test_uuid_keys_use_reinterpret_as_uint64(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed", vars={"rows_per_bucket": 2}))
    flush_logs(sources)

    assert query_log(sources, "reinterpretAsUInt64(key)")
    assert query_log(sources, "reinterpretAsUInt64(key) % 3 = 0")


@pytest.mark.parametrize("table", ["raw_responses_int", "raw_responses_uint"])
def test_integer_keys_build(sources, dbt, table):
    seed_int(sources, table)
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={
            "source_name": table,
            "bucket_source_table": f"{SCHEMA}.{table}",
        },
    )
    assert_ok(result)

    rows = sources.query(
        f"select toString(key), payload from {SCHEMA}.bucketed order by key"
    ).result_rows
    assert rows == [("1", "a2"), ("2", "b1"), ("3", "c1")]


def test_negative_integer_keys_abort(sources, dbt):
    sources.insert(
        f"{SCHEMA}.raw_responses_negative",
        [(-1, "a1", T1, 1, 0), (1, "b1", T1, 1, 0)],
        column_names=COLUMNS,
    )
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={
            "source_name": "raw_responses_negative",
            "bucket_source_table": f"{SCHEMA}.raw_responses_negative",
        },
    )
    assert_failed(result, 'has 1 negative values in "key"')


def test_unsupported_key_type_aborts(sources, dbt):
    sources.insert(
        f"{SCHEMA}.raw_responses_text",
        [("a", "a1", T1, 1, 0)],
        column_names=COLUMNS,
    )
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={
            "source_name": "raw_responses_text",
            "bucket_source_table": f"{SCHEMA}.raw_responses_text",
        },
    )
    assert_failed(result, 'bucket_key_column "key" has unsupported type "String"')


def test_source_missing_aborts(sources, dbt):
    result = dbt("run", "--select", "bucketed", vars={"bucket_source_table": f"{SCHEMA}.missing"})
    assert_failed(result, 'bucket_source_table "test.missing" not found for model "bucketed"')


def test_source_not_a_table_aborts(sources, seed_responses, dbt):
    sources.command(
        f"create view {SCHEMA}.raw_responses_view as select * from {SCHEMA}.raw_responses"
    )
    result = dbt(
        "run", "--select", "bucketed", vars={"bucket_source_table": f"{SCHEMA}.raw_responses_view"}
    )
    assert_failed(result, "must be a table, got type")


def test_missing_key_column_aborts(sources, seed_responses, dbt):
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={"bucket_key_column": "missing", "unique_key": "missing"},
    )
    assert_failed(result, 'bucket_key_column "missing" not found in bucket_source_table')


def test_missing_snapshot_column_aborts(sources, seed_responses, dbt):
    result = dbt("run", "--select", "bucketed", vars={"bucket_snapshot_column": "missing_snapshot"})
    assert_failed(
        result, 'bucket_snapshot_column "missing_snapshot" not found in bucket_source_table'
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_keys.py -v`

Expected: FAIL with ClickHouse errors about unknown columns or invalid modulo expressions; the negative-key case fails without the count check.

- [ ] **Step 3: Add the source checks**

In `macros/materializations/bucketed_incremental.sql`, replace the provisional uuid-only predicate with the probe. Replace:

```jinja
    {% set bucket_lhs = 'reinterpretAsUInt64(' ~ bucket_key_column ~ ')' %}
    {% set build_relation = intermediate_relation %}
    {% set count_sql %}select count() as row_count from {{ bucket_source_table }}{% endset %}
```

with:

```jinja
    {% set build_relation = intermediate_relation %}
    {% set bucket_parts = bucket_source_table.split('.') %}
    {% set bucket_relation = adapter.get_relation(
        database=bucket_parts[0], schema=bucket_parts[0], identifier=bucket_parts[1]
    ) %}
    {% if bucket_relation is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
          ~ '" not found for model "' ~ model.name ~ '".'
      ) }}
    {% endif %}
    {% if bucket_relation.type != 'table' %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
          ~ '" must be a table, got type "' ~ bucket_relation.type ~ '".'
      ) }}
    {% endif %}
    {% set ns = namespace(bucket_dtype=none, snapshot_dtype=none) %}
    {% for col in adapter.get_columns_in_relation(bucket_relation) %}
      {% if col.name == bucket_key_column %}
        {% set ns.bucket_dtype = col.dtype %}
      {% endif %}
      {% if col.name == bucket_snapshot_column %}
        {% set ns.snapshot_dtype = col.dtype %}
      {% endif %}
    {% endfor %}
    {% if ns.bucket_dtype is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_key_column "' ~ bucket_key_column
          ~ '" not found in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% if ns.bucket_dtype == 'UUID' %}
      {% set bucket_key_type = 'uuid' %}
    {% elif ns.bucket_dtype in ('Int8', 'Int16', 'Int32', 'Int64', 'Int128', 'Int256') %}
      {% set bucket_key_type = 'int' %}
    {% elif ns.bucket_dtype in ('UInt8', 'UInt16', 'UInt32', 'UInt64', 'UInt128', 'UInt256') %}
      {% set bucket_key_type = 'uint' %}
    {% else %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_key_column "' ~ bucket_key_column
          ~ '" has unsupported type "' ~ ns.bucket_dtype
          ~ '"; expected a non-null UUID, signed integer or unsigned integer column.'
      ) }}
    {% endif %}
    {% if bucket_key_type == 'uuid' %}
      {% set bucket_lhs = 'reinterpretAsUInt64(' ~ bucket_key_column ~ ')' %}
    {% else %}
      {% set bucket_lhs = bucket_key_column %}
    {% endif %}
    {% if ns.snapshot_dtype is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" not found in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% set count_sql %}select count() as row_count from {{ bucket_source_table }}{% endset %}
```

- [ ] **Step 4: Add the negative key count**

Replace:

```jinja
    {% set count_sql %}select count() as row_count from {{ bucket_source_table }}{% endset %}
    {% set count_result = run_query(count_sql) %}
    {% set row_count = count_result.columns[0].values()[0] | int %}
```

with:

```jinja
    {% set count_select = ['count() as row_count'] %}
    {% if bucket_key_type in ('int', 'uint') %}
      {% do count_select.append('countIf(' ~ bucket_key_column ~ ' < 0) as negative_key_count') %}
    {% endif %}
    {% set count_sql %}select {{ count_select | join(', ') }} from {{ bucket_source_table }}{% endset %}
    {% set count_result = run_query(count_sql) %}
    {% set row_count = count_result.columns[0].values()[0] | int %}
    {% if bucket_key_type in ('int', 'uint') %}
      {% set negative_key_count = count_result.columns[1].values()[0] | int %}
      {% if negative_key_count > 0 %}
        {{ exceptions.raise_compiler_error(
            'bucketed_incremental: bucket_source_table "' ~ bucket_source_table
            ~ '" has ' ~ negative_key_count ~ ' negative values in "' ~ bucket_key_column
            ~ '". Negative integer keys cannot be bucketed (the bucket maths uses '
            ~ 'modulo), so a full refresh would silently drop them. Use a '
            ~ 'non-negative key column.'
        ) }}
      {% endif %}
    {% endif %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_keys.py -v`

Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/tests/test_keys.py
git commit -m "feat: validate key and source types for bucketed_incremental"
```

---

### Task 7: Snapshot Pinning

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Create: `integration_tests/tests/test_snapshot.py`

**Interfaces:**
- Consumes: Task 6's source checks.
- Produces: `S0` capture in the count query, a `snapshot_max` run log field, the `DateTime64(9)` dtype check with timezone handling, and a `<= S0` bound on every bucket predicate.

- [ ] **Step 1: Write the failing tests**

Create `integration_tests/tests/test_snapshot.py`:

```python
from datetime import datetime

from integration_tests.tests.conftest import (
    COLUMNS,
    SCHEMA,
    T1,
    U1,
    assert_failed,
    assert_ok,
    create_source,
    flush_logs,
    query_log,
)


def test_snapshot_bound_applies_to_every_bucket(sources, seed_responses, dbt):
    started = datetime.now()
    assert_ok(dbt("run", "--select", "bucketed", vars={"rows_per_bucket": 2}))
    flush_logs(sources)

    bounds = query_log(
        sources,
        "_peerdb_synced_at <= toDateTime64('2026-01-02 00:00:00.000000000', 9)",
        since=started,
    )
    assert len(bounds) == 3


def test_snapshot_max_is_logged(sources, seed_responses, dbt):
    result = dbt("run", "--select", "bucketed")
    assert_ok(result)

    assert "snapshot_max=2026-01-02 00:00:00.000000000" in result.stdout


def test_snapshot_timezone_is_preserved(sources, dbt):
    create_source(sources, "raw_responses_tz", snapshot_type="DateTime64(9, 'UTC')")
    sources.insert(f"{SCHEMA}.raw_responses_tz", [(U1, "a1", T1, 1, 0)], column_names=COLUMNS)

    started = datetime.now()
    assert_ok(
        dbt(
            "run",
            "--select",
            "bucketed",
            vars={
                "source_name": "raw_responses_tz",
                "bucket_source_table": f"{SCHEMA}.raw_responses_tz",
            },
        )
    )
    flush_logs(sources)

    assert query_log(
        sources, "toDateTime64('2026-01-01 00:00:00.000000000', 9, 'UTC')", since=started
    )


def test_snapshot_type_must_be_datetime64_9(sources, dbt):
    sources.insert(
        f"{SCHEMA}.raw_responses_bad_snapshot", [(U1, "a1", "nope", 1, 0)], column_names=COLUMNS
    )
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={
            "source_name": "raw_responses_bad_snapshot",
            "bucket_source_table": f"{SCHEMA}.raw_responses_bad_snapshot",
        },
    )
    assert_failed(
        result, 'bucket_snapshot_column "_peerdb_synced_at" has unsupported type "String"'
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_snapshot.py -v`

Expected: FAIL because no bucket predicate carries a snapshot bound, the log has no `snapshot_max`, and the `String` snapshot column builds without complaint.

- [ ] **Step 3: Add the snapshot dtype check**

In `macros/materializations/bucketed_incremental.sql`, replace:

```jinja
    {% if ns.snapshot_dtype is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" not found in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
```

with:

```jinja
    {% if ns.snapshot_dtype is none %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" not found in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% if not modules.re.match("^DateTime64[(]9(, *'[^']+')?[)]$", ns.snapshot_dtype) %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" has unsupported type "' ~ ns.snapshot_dtype
          ~ '"; it must be a non-null DateTime64(9) column.'
      ) }}
    {% endif %}
    {% set snapshot_tz = ns.snapshot_dtype.split("'")[1] if "'" in ns.snapshot_dtype else none %}
```

- [ ] **Step 4: Capture `S0` and bound every bucket**

Replace:

```jinja
    {% set count_select = ['count() as row_count'] %}
    {% if bucket_key_type in ('int', 'uint') %}
      {% do count_select.append('countIf(' ~ bucket_key_column ~ ' < 0) as negative_key_count') %}
    {% endif %}
    {% set count_sql %}select {{ count_select | join(', ') }} from {{ bucket_source_table }}{% endset %}
```

with:

```jinja
    {% set count_select = ['count() as row_count'] %}
    {% if bucket_key_type in ('int', 'uint') %}
      {% do count_select.append('countIf(' ~ bucket_key_column ~ ' < 0) as negative_key_count') %}
      {% set snapshot_idx = 2 %}
    {% else %}
      {% set snapshot_idx = 1 %}
    {% endif %}
    {% do count_select.append('toString(max(' ~ bucket_snapshot_column ~ ')) as snapshot_max') %}
    {% set count_sql %}select {{ count_select | join(', ') }} from {{ bucket_source_table }}{% endset %}
```

Then, after the negative key check, insert:

```jinja
    {% set snapshot_str = count_result.columns[snapshot_idx].values()[0] %}
    {% if snapshot_str is none or snapshot_str|trim|length == 0 %}
      {% set snapshot_str = none %}
    {% endif %}
    {% if snapshot_str is none and row_count > 0 %}
      {{ exceptions.raise_compiler_error(
          'bucketed_incremental: bucket_snapshot_column "' ~ bucket_snapshot_column
          ~ '" has no usable maximum in bucket_source_table "' ~ bucket_source_table ~ '".'
      ) }}
    {% endif %}
    {% if snapshot_str is none %}
      {% set snapshot_literal = none %}
    {% else %}
      {% set snapshot_literal = "toDateTime64('" ~ snapshot_str ~ "', 9" ~ (", '" ~ snapshot_tz ~ "'" if snapshot_tz else "") ~ ")" %}
    {% endif %}
```

Update the log line. Replace:

```jinja
    {{ log(
        'bucketed_incremental: row_count=' ~ row_count ~ ' bucket_count=' ~ bucket_count, info=True
    ) }}
```

with:

```jinja
    {{ log(
        'bucketed_incremental: row_count='
        ~ row_count
        ~ ' bucket_count='
        ~ bucket_count
        ~ ' snapshot_max='
        ~ (snapshot_str or 'n/a'),
        info=True,
    ) }}
```

Update the predicate. Replace:

```jinja
      {% set predicate = 'where ' ~ bucket_lhs ~ ' % ' ~ bucket_count ~ ' = ' ~ i %}
```

with:

```jinja
      {% set predicate = 'where '
          ~ bucket_lhs
          ~ ' % '
          ~ bucket_count
          ~ ' = '
          ~ i
          ~ ' and '
          ~ bucket_snapshot_column
          ~ ' <= '
          ~ snapshot_literal %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_snapshot.py -v`

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/tests/test_snapshot.py
git commit -m "feat: pin bucketed builds to a captured snapshot bound"
```

---

### Task 8: Concurrent-Write Detection

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Create: `integration_tests/models/bucketed_slow.sql`
- Create: `integration_tests/tests/test_concurrent_writes.py`

**Interfaces:**
- Consumes: Task 7's `snapshot_literal` and `snapshot_str`, plus `BackgroundWriter` from Task 1.
- Produces: the post-loop detection query and the `error` / `warn` / `ignore` responses; the `bucketed_slow` model with `rows_per_bucket=4` over eight keys.

- [ ] **Step 1: Write the failing tests**

Create `integration_tests/tests/test_concurrent_writes.py`:

```python
from datetime import datetime

import pytest

from integration_tests.tests.conftest import (
    COLUMNS,
    SCHEMA,
    T1,
    U1,
    BackgroundWriter,
    assert_failed,
    assert_ok,
    flush_logs,
    query_log,
)

SLOW_KEYS = [f"00000000-0000-0000-0000-0000000000{i:02d}" for i in range(1, 9)]


@pytest.fixture
def seed_slow(sources):
    rows = [(key, f"p{index}", T1, 1, 0) for index, key in enumerate(SLOW_KEYS)]
    sources.insert(f"{SCHEMA}.raw_responses_slow", rows, column_names=COLUMNS)
    return rows


def target_rows(sources):
    return {
        row[0]: row[1]
        for row in sources.query(
            f"select toString(key), payload from {SCHEMA}.bucketed_slow"
        ).result_rows
    }


def test_error_stops_before_publish(sources, seed_slow, dbt):
    assert_ok(dbt("run", "--select", "bucketed_slow", vars={"on_concurrent_writes": "ignore"}))

    with BackgroundWriter(
        sources, "raw_responses_slow", U1, wait_for="_peerdb_synced_at <= toDateTime64"
    ):
        result = dbt(
            "run",
            "--full-refresh",
            "--select",
            "bucketed_slow",
            vars={"on_concurrent_writes": "error"},
        )

    assert_failed(result, "concurrent writes to bucket_source_table")
    assert target_rows(sources) == {key: f"p{index}" for index, key in enumerate(SLOW_KEYS)}


def test_warn_publishes_and_excludes_the_write(sources, seed_slow, dbt):
    assert_ok(dbt("run", "--select", "bucketed_slow", vars={"on_concurrent_writes": "ignore"}))

    with BackgroundWriter(
        sources, "raw_responses_slow", U1, wait_for="_peerdb_synced_at <= toDateTime64"
    ):
        result = dbt(
            "run",
            "--full-refresh",
            "--select",
            "bucketed_slow",
            vars={"on_concurrent_writes": "warn"},
        )

    assert_ok(result)
    assert "WARNING: concurrent writes to bucket_source_table" in result.stdout
    assert target_rows(sources) == {key: f"p{index}" for index, key in enumerate(SLOW_KEYS)}


def test_ignore_skips_the_detection_query(sources, seed_slow, dbt):
    started = datetime.now()
    with BackgroundWriter(
        sources, "raw_responses_slow", U1, wait_for="_peerdb_synced_at <= toDateTime64"
    ):
        result = dbt("run", "--select", "bucketed_slow", vars={"on_concurrent_writes": "ignore"})

    assert_ok(result)
    flush_logs(sources)
    assert query_log(sources, "writes_detected", since=started) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_concurrent_writes.py -v`

Expected: FAIL because the `bucketed_slow` model does not exist and the macro never detects the mid-build write.

- [ ] **Step 3: Create the slow model**

Create `integration_tests/models/bucketed_slow.sql`:

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy="delete_insert",
    unique_key="key",
    order_by="key",
    bucket_key_column="key",
    bucket_source_table=env_var("CLICKHOUSE_SCHEMA", "test") ~ ".raw_responses_slow",
    bucket_snapshot_column="_peerdb_synced_at",
    rows_per_bucket=4,
    on_concurrent_writes=var("on_concurrent_writes", "error")
) }}

with raw_versions as (
    select key, payload, _peerdb_synced_at
    from {{ source("test", "raw_responses_slow") }}
    -- __BUCKET_PREDICATE__
),

deduped as (
    select key, payload, _peerdb_synced_at
    from raw_versions
    order by _peerdb_synced_at desc
    limit 1 by key
)

select key, payload, _peerdb_synced_at
from (
    select key, payload, _peerdb_synced_at, sleepEachRow(0.25) as _sleep
    from deduped
)
where _sleep >= 0
```

The `where _sleep >= 0` filter keeps the sleep expression from being pruned and is always true. If the server refuses a non-deterministic function in a filter, move the expression into the subquery select and filter on the column there.

- [ ] **Step 4: Add the detection block**

In `macros/materializations/bucketed_incremental.sql`, insert between `{% endif %}` (the closing of the empty/bucket branch) and `{% do adapter.rename_relation(intermediate_relation, target_relation) %}`:

```jinja
    {% if snapshot_str is not none and on_concurrent_writes != 'ignore' %}
      {% set post_count_sql %}select max({{ bucket_snapshot_column }}) > {{ snapshot_literal }} as writes_detected from {{ bucket_source_table }}{% endset %}
      {% set post_count_result = run_query(post_count_sql) %}
      {% set writes_detected = post_count_result.columns[0].values()[0] %}
      {% if writes_detected %}
        {% if on_concurrent_writes == 'error' %}
          {{ exceptions.raise_compiler_error(
              'bucketed_incremental: concurrent writes to bucket_source_table "' ~ bucket_source_table
              ~ '" detected during the rebuild (snapshot bound ' ~ snapshot_str ~ ' exceeded). '
              ~ 'Re-run against a quiesced source; the previous table was left untouched.'
          ) }}
        {% else %}
          {{ log(
              'bucketed_incremental: WARNING: concurrent writes to bucket_source_table "'
              ~ bucket_source_table
              ~ '" detected during the rebuild; run an incremental afterwards to converge.',
              info=True,
          ) }}
        {% endif %}
      {% endif %}
    {% endif %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_concurrent_writes.py -v`

Expected: 3 passed. Each run takes a few seconds because `sleepEachRow(0.25)` slows both buckets.

- [ ] **Step 6: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/models/bucketed_slow.sql integration_tests/tests/test_concurrent_writes.py
git commit -m "feat: detect concurrent writes during bucketed rebuilds"
```

---

### Task 9: Atomic Publication and Lifecycle

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Modify: `integration_tests/models/bucketed.sql`
- Create: `integration_tests/tests/test_publish.py`

**Interfaces:**
- Consumes: Task 8's full-refresh branch.
- Produces: the backup relation, the three publish shapes, the `to_drop` cleanup after commit, grants, persisted docs and index creation; `pre_hook` and `post_hook` config vars on the `bucketed` model.

- [ ] **Step 1: Write the failing tests**

Create `integration_tests/tests/test_publish.py`:

```python
from datetime import datetime

from integration_tests.tests.conftest import SCHEMA, assert_failed, assert_ok, flush_logs, query_log


def test_rebuild_uses_exchange(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))

    started = datetime.now()
    assert_ok(dbt("run", "--select", "bucketed", "--full-refresh"))
    flush_logs(sources)

    assert query_log(sources, "EXCHANGE TABLES", since=started)


def test_rebuild_keeps_target_contents(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))
    assert_ok(dbt("run", "--select", "bucketed", "--full-refresh"))

    assert sources.query(f"select count() from {SCHEMA}.bucketed").result_rows == [(5,)]


def test_failed_rebuild_leaves_target_untouched(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))
    sources.command(f"drop table {SCHEMA}.raw_responses")

    result = dbt("run", "--select", "bucketed", "--full-refresh")

    assert_failed(result, 'bucket_source_table "test.raw_responses" not found for model "bucketed"')
    assert sources.query(f"select count() from {SCHEMA}.bucketed").result_rows == [(5,)]


def test_backup_is_dropped_after_publish(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))
    assert_ok(dbt("run", "--select", "bucketed", "--full-refresh"))

    backups = sources.query(
        f"select count() from system.tables where database = '{SCHEMA}' and name like '%__dbt_backup'"
    ).result_rows
    assert backups == [(0,)]


def test_pre_and_post_hooks_run(sources, seed_responses, dbt):
    sources.command(
        f"create table {SCHEMA}.hook_log (event String) engine = MergeTree() order by event"
    )
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={
            "pre_hook": f"insert into {SCHEMA}.hook_log values ('pre')",
            "post_hook": f"insert into {SCHEMA}.hook_log values ('post')",
        },
    )
    assert_ok(result)

    rows = sources.query(f"select event from {SCHEMA}.hook_log order by event").result_rows
    assert rows == [("post",), ("pre",)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_publish.py -v`

Expected: the exchange and backup tests FAIL (no `EXCHANGE TABLES`, no backup logic); the hook test FAILS because the model has no hook config.

- [ ] **Step 3: Add backup handling and the publish shapes**

In `macros/materializations/bucketed_incremental.sql`, replace:

```jinja
  {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation) -%}

  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}
```

with:

```jinja
  {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
  {%- set backup_relation_type = 'table' if existing_relation is none else existing_relation.type -%}
  {%- set backup_relation = make_backup_relation(target_relation, backup_relation_type) -%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation) -%}
  {%- set preexisting_backup_relation = load_cached_relation(backup_relation) -%}

  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}
  {{ drop_relation_if_exists(preexisting_backup_relation) }}
```

Then replace:

```jinja
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% if full_refresh_mode %}
```

with:

```jinja
  {{ run_hooks(pre_hooks, inside_transaction=True) }}
  {% set to_drop = [] %}
  {% set need_swap = false %}

  {% if full_refresh_mode %}
```

Replace the first-run rename:

```jinja
    {% do adapter.rename_relation(intermediate_relation, target_relation) %}
  {% else %}
```

with:

```jinja
    {% set need_swap = true %}
  {% else %}
```

Then replace the tail:

```jinja
  {% endif %}

  {% do persist_docs(target_relation, model) %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {% do adapter.commit() %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}
```

with:

```jinja
  {% endif %}

  {% if need_swap %}
      {% if existing_relation is none %}
        {% do adapter.rename_relation(intermediate_relation, target_relation) %}
      {% elif existing_relation.can_exchange %}
        {% do adapter.rename_relation(intermediate_relation, backup_relation) %}
        {% do exchange_tables_atomic(backup_relation, target_relation) %}
        {% do to_drop.append(backup_relation) %}
      {% else %}
        {% do adapter.rename_relation(target_relation, backup_relation) %}
        {% do adapter.rename_relation(intermediate_relation, target_relation) %}
        {% do to_drop.append(backup_relation) %}
      {% endif %}
  {% endif %}

  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}

  {% do persist_docs(target_relation, model) %}

  {% if existing_relation is none or existing_relation.is_view or should_full_refresh() %}
    {% do create_indexes(target_relation) %}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {% do adapter.commit() %}

  {% for rel in to_drop %}
      {% do adapter.drop_relation(rel) %}
  {% endfor %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}
```

- [ ] **Step 4: Add hook config to the model**

In `integration_tests/models/bucketed.sql`, replace:

```sql
    on_schema_change=var("on_schema_change", "ignore")
) }}
```

with:

```sql
    on_schema_change=var("on_schema_change", "ignore"),
    pre_hook=var("pre_hook", none),
    post_hook=var("post_hook", none)
) }}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_publish.py -v`

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/models/bucketed.sql integration_tests/tests/test_publish.py
git commit -m "feat: publish bucketed rebuilds atomically and finish the dbt lifecycle"
```

---

### Task 10: Incremental Maintenance

**Files:**
- Modify: `macros/materializations/bucketed_incremental.sql`
- Modify: `integration_tests/models/bucketed.sql`
- Create: `integration_tests/tests/test_incremental.py`

**Interfaces:**
- Consumes: Task 9's materialization and model.
- Produces: the incremental branch (schema changes plus `clickhouse__incremental_delete_insert`); the `extra_column` and `predicates` config vars on the `bucketed` model.

- [ ] **Step 1: Write the failing tests**

Create `integration_tests/tests/test_incremental.py`:

```python
from datetime import datetime

from integration_tests.tests.conftest import (
    COLUMNS,
    SCHEMA,
    T2,
    T3,
    U2,
    U3,
    assert_ok,
    flush_logs,
    query_log,
)

U6 = "00000000-0000-0000-0000-000000000006"


def rows_by_key(sources):
    rows = sources.query(f"select toString(key), payload, path from {SCHEMA}.bucketed").result_rows
    return {row[0]: (row[1], row[2]) for row in rows}


def test_incremental_replaces_touched_keys(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))

    sources.insert(
        f"{SCHEMA}.raw_responses",
        [(U2, "b2", T3, 3, 0), (U6, "f1", T3, 1, 0)],
        column_names=COLUMNS,
    )
    assert_ok(dbt("run", "--select", "bucketed"))

    rows = rows_by_key(sources)
    assert rows[U2] == ("b2", "incr")
    assert rows[U6] == ("f1", "incr")
    assert rows[U3] == ("c1", "full")


def test_incremental_recaptures_a_tie(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))

    sources.insert(f"{SCHEMA}.raw_responses", [(U3, "c2", T2, 2, 0)], column_names=COLUMNS)
    assert_ok(dbt("run", "--select", "bucketed"))

    assert rows_by_key(sources)[U3] == ("c2", "incr")


def test_on_schema_change_appends_new_columns(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))

    sources.command(f"alter table {SCHEMA}.raw_responses add column extra String default ''")
    sources.insert(
        f"{SCHEMA}.raw_responses",
        [(U3, "c3", T3, 3, 0, "x")],
        column_names=COLUMNS + ["extra"],
    )
    result = dbt(
        "run",
        "--select",
        "bucketed",
        vars={"extra_column": True, "on_schema_change": "append_new_columns"},
    )
    assert_ok(result)

    assert sources.query(
        f"select name from system.columns where database = '{SCHEMA}' and table = 'bucketed' and name = 'extra'"
    ).result_rows == [("extra",)]
    assert sources.query(
        f"select extra from {SCHEMA}.bucketed where toString(key) = '{U3}'"
    ).result_rows == [("x",)]


def test_incremental_predicates_reach_the_delete(sources, seed_responses, dbt):
    assert_ok(dbt("run", "--select", "bucketed"))

    started = datetime.now()
    assert_ok(dbt("run", "--select", "bucketed", vars={"predicates": ["1 = 1"]}))
    flush_logs(sources)

    assert query_log(sources, "and 1 = 1", since=started)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest integration_tests/tests/test_incremental.py -v`

Expected: every test FAILS with `bucketed_incremental: incremental runs are not implemented yet.`

- [ ] **Step 3: Implement the incremental branch**

In `macros/materializations/bucketed_incremental.sql`, replace:

```jinja
  {% else %}
    {{ exceptions.raise_compiler_error(
        'bucketed_incremental: incremental runs are not implemented yet.'
    ) }}
  {% endif %}
```

with:

```jinja
  {% else %}
    {%- if on_schema_change != 'ignore' %}
      {%- set column_changes = adapter.check_incremental_schema_changes(on_schema_change, existing_relation, sql, query_settings=config.get('query_settings', {})) -%}
      {% if column_changes %}
        {% do clickhouse__apply_column_changes(column_changes, existing_relation) %}
        {% set existing_relation = load_cached_relation(this) %}
      {% endif %}
    {% endif %}
    {% do clickhouse__incremental_delete_insert(
        existing_relation, unique_key, incremental_predicates
    ) %}
  {% endif %}
```

- [ ] **Step 4: Add the extra column and predicates to the model**

Replace `integration_tests/models/bucketed.sql` with:

```sql
{{ config(
    materialized="bucketed_incremental",
    incremental_strategy=var("incremental_strategy", "delete_insert"),
    engine=var("engine", "MergeTree()"),
    unique_key=var("unique_key", "key"),
    order_by=var("order_by", "key"),
    bucket_key_column=var("bucket_key_column", "key"),
    bucket_source_table=var("bucket_source_table", env_var("CLICKHOUSE_SCHEMA", "test") ~ ".raw_responses"),
    bucket_snapshot_column=var("bucket_snapshot_column", "_peerdb_synced_at"),
    rows_per_bucket=var("rows_per_bucket", 1000000),
    on_concurrent_writes=var("on_concurrent_writes", "error"),
    inserts_only=var("inserts_only", false),
    on_schema_change=var("on_schema_change", "ignore"),
    predicates=var("predicates", []),
    pre_hook=var("pre_hook", none),
    post_hook=var("post_hook", none)
) }}

{% set key = var("bucket_key_column", "key") %}
{% set source_name = var("source_name", "raw_responses") %}

with raw_versions as (
    select
        {{ key }} as key,
        payload,
        _peerdb_synced_at{% if var("extra_column", false) %}, extra{% endif %}
    from {{ source("test", source_name) }}
    {% if is_incremental() %}
    where {{ key }} in (
        select distinct {{ key }}
        from {{ source("test", source_name) }}
        where _peerdb_synced_at >= (
            select coalesce(
                max(_peerdb_synced_at),
                toDateTime64('1970-01-01 00:00:00.000000000', 9)
            )
            from {{ this }}
        )
    )
    {% else %}
    -- __BUCKET_PREDICATE__
    {% endif %}
),

deduped as (
    select
        key,
        payload,
        _peerdb_synced_at{% if var("extra_column", false) %}, extra{% endif %}
    from raw_versions
    order by _peerdb_synced_at desc
    limit 1 by key
)

select
    key,
    payload,
    _peerdb_synced_at{% if var("extra_column", false) %}, extra{% endif %},
    {% if is_incremental() %}'incr'{% else %}'full'{% endif %} as path
from deduped
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest integration_tests/tests/test_incremental.py -v`

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add macros/materializations/bucketed_incremental.sql integration_tests/models/bucketed.sql integration_tests/tests/test_incremental.py
git commit -m "feat: maintain bucketed_incremental targets with delete+insert"
```

---

### Task 11: Conformance Sweep

**Files:**
- Modify: any file a failing test exposes.
- Review: `README.md`, `docs/bucketed_incremental/spec.md`, `docs/bucketed_incremental/design.md`.

**Interfaces:**
- Consumes: every earlier task.
- Produces: a green suite and a written mapping from spec requirements to tests.

- [ ] **Step 1: Run the whole suite**

Run: `make test`

Expected: all tests pass.

- [ ] **Step 2: Check the spec against the tests**

Walk `docs/bucketed_incremental/spec.md` requirement by requirement and point to the test that proves it:

| Requirement | Test |
|-------------|------|
| Incremental Detection | `test_incremental.py` path assertions |
| Bucketed Full Refresh | `test_full_refresh.py` |
| Bucket Predicate Marker | `test_marker.py`, `test_full_refresh.py` bucket logs |
| Bucket Sizing | `test_full_refresh.py` |
| Empty Source | `test_marker.py` |
| Configuration Validation | `test_validation.py` |
| Strategy Enforcement | `test_validation.py` |
| Key Column Contract | `test_keys.py` |
| Source Table Contract | `test_keys.py` |
| Snapshot Column Contract | `test_snapshot.py` |
| Concurrent-Write Detection | `test_concurrent_writes.py` |
| Atomic Publication | `test_publish.py` |
| Incremental Maintenance | `test_incremental.py` |
| Model Contract | `test_incremental.py` tie test; `integration_tests/models/bucketed.sql` |

Fix any gap by adding the missing test to the owning task's file, then rerun `make test`.

- [ ] **Step 3: Check the rendered SQL parses cleanly**

Run: `uv run dbt parse --project-dir integration_tests --profiles-dir integration_tests`

Expected: parse completes without errors.

- [ ] **Step 4: Update the README only if behavior drifted**

Compare `README.md` with the rebuilt macro. If a config name, default or error message changed, update the README and `docs/bucketed_incremental/design.md`. If nothing changed, leave both files alone.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: verify bucketed_incremental against the specification"
```

---

## Self-Review

- **Spec coverage:** every requirement in `docs/bucketed_incremental/spec.md` maps to a test in Task 11 Step 2. The Model Contract is partly documentation; the tie case is the only executable part.
- **Placeholders:** none. Every step shows the exact file, code and command.
- **Type consistency:** the materialization references `snapshot_literal`, `snapshot_str`, `incremental_predicates`, `bucket_lhs`, `build_relation`, `to_drop` and `need_swap`; all are defined in the task that first uses them. Fixture names (`client`, `dbt`, `sources`, `seed_responses`, `BackgroundWriter`, `create_source`, `flush_logs`, `query_log`) are created in Task 1 and reused unchanged.
- **Review Focus:** each of the six lines has a home. Physical deletes and backdated writes are documented, not tested; the empty-source and engine-fallback cases have tests or explicit notes; the dedupe contract appears in the spec and the model.
