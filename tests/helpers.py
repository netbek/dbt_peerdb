from __future__ import annotations

from .conftest import IntegrationTest
from clickhouse_connect.driver.client import Client
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from dbt_common.events.base_types import EventMsg
from dw_lib.database import ClickHouseSettings
from dw_lib.dbt import Dbt, DbtInvocationResult
from types import TracebackType
from typing import Any, Self

import clickhouse_connect
import pytest
import re
import threading
import time

SOURCE_TABLE = "bi_source"
SOURCE_COLUMNS = [
    "id",
    "payload",
    "region",
    "_peerdb_synced_at",
    "_peerdb_is_deleted",
    "_peerdb_version",
]

# Low-cardinality demo column: 3 values cycled per key, so partitioning by it stays bounded.
REGIONS = ["af-south", "eu-west", "us-east"]


def region_for(key: int) -> str:
    """Deterministic region for a key, stable across versions of the same row."""
    return REGIONS[key % len(REGIONS)]


SNAPSHOT_BASE = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)


def snapshot_at(seconds: int) -> datetime:
    """Return a deterministic snapshot timestamp offset from SNAPSHOT_BASE."""
    return SNAPSHOT_BASE + timedelta(seconds=seconds)


def base_rows() -> list[tuple]:
    """Ten source rows, where id 1 carries a second, newer version."""
    rows = [(i, f"value-{i}", region_for(i), snapshot_at(i), 0, 1) for i in range(10)]
    rows.append((1, "value-1-v2", region_for(1), snapshot_at(20), 0, 2))
    return rows


def expected_base_rows() -> list[tuple]:
    """The deduped rows base_rows() should produce in the target."""
    return [(i, "value-1-v2" if i == 1 else f"value-{i}") for i in range(10)]


def make_client(settings: ClickHouseSettings) -> Client:
    """Open a clickhouse_connect client from the dbt profile settings."""
    return clickhouse_connect.get_client(
        host=settings.host,
        port=settings.port,
        username=settings.username,
        password=settings.password,
        database=settings.database,
    )


def query_scalar(
    clickhouse_client: Client, sql: str, parameters: dict[str, Any] | None = None
) -> Any:
    """Run a query and return the first column of its first row."""
    return clickhouse_client.query(sql, parameters=parameters).first_row[0]


def fetch_rows(
    clickhouse_client: Client, sql: str, parameters: dict[str, Any] | None = None
) -> list[tuple]:
    """Run a query and return all rows as plain tuples."""
    return [tuple(row) for row in clickhouse_client.query(sql, parameters=parameters).result_rows]


def relation_names(clickhouse_client: Client) -> list[str]:
    """Names of every relation in the client's database."""
    return [
        row[0]
        for row in fetch_rows(
            clickhouse_client,
            "select name from system.tables where database = currentDatabase() order by name",
        )
    ]


def relation_exists(clickhouse_client: Client, name: str) -> bool:
    """Whether a relation of the given name exists in the client's database."""
    return bool(
        fetch_rows(
            clickhouse_client,
            "select 1 from system.tables where database = currentDatabase() and name = {name:String}",
            {"name": name},
        )
    )


def table_engine(clickhouse_client: Client, name: str) -> str | None:
    """Engine of the named relation, or None if it does not exist."""
    rows = fetch_rows(
        clickhouse_client,
        "select engine from system.tables where database = currentDatabase() and name = {name:String}",
        {"name": name},
    )
    return rows[0][0] if rows else None


def table_partition_key(clickhouse_client: Client, name: str) -> str | None:
    """Partition key of the named relation, or None if it does not exist."""
    rows = fetch_rows(
        clickhouse_client,
        "select partition_key from system.tables where database = currentDatabase() "
        "and name = {name:String}",
        {"name": name},
    )
    return rows[0][0] if rows else None


def column_names(clickhouse_client: Client, table: str) -> list[str]:
    """Column names of a table, in definition order."""
    return [
        row[0]
        for row in fetch_rows(
            clickhouse_client,
            "select name from system.columns where database = currentDatabase() and table = {table:String} "
            "order by position",
            {"table": table},
        )
    ]


def drop_all_relations(clickhouse_client: Client) -> None:
    """Drop every relation in the client's database, dispatching on its engine."""
    relations = fetch_rows(
        clickhouse_client,
        "select name, engine from system.tables where database = currentDatabase()",
    )

    for name, engine in relations:
        if engine == "View":
            clickhouse_client.command(f"drop view if exists {clickhouse_client.database}.{name}")

    for name, engine in relations:
        if engine == "View":
            continue
        if engine == "Dictionary":
            clickhouse_client.command(
                f"drop dictionary if exists {clickhouse_client.database}.{name}"
            )
        else:
            clickhouse_client.command(f"drop table if exists {clickhouse_client.database}.{name}")


def create_source(
    clickhouse_client: Client,
    *,
    key_type: str = "UInt64",
    snapshot_type: str = "DateTime64(9)",
    table: str = SOURCE_TABLE,
    include_key: bool = True,
    include_snapshot: bool = True,
) -> None:
    """Create or replace the standard source table, optionally varying the key and snapshot types or
    omitting either column."""
    definitions: list[str] = []
    if include_key:
        definitions.append(f"id {key_type}")
    definitions.append("payload String")
    definitions.append("region LowCardinality(String)")
    if include_snapshot:
        definitions.append(f"_peerdb_synced_at {snapshot_type}")
    definitions.extend(["_peerdb_is_deleted Int8", "_peerdb_version Int64"])

    clickhouse_client.command(
        f"create or replace table {clickhouse_client.database}.{table} ({', '.join(definitions)}) "
        "engine MergeTree order by tuple()"
    )


def insert_rows(
    clickhouse_client: Client,
    rows: Sequence[Sequence[Any]],
    *,
    table: str = SOURCE_TABLE,
    columns: Sequence[str] = SOURCE_COLUMNS,
) -> None:
    """Insert rows into the source table, using the standard columns by default."""
    if not rows:
        return
    clickhouse_client.insert(
        f"{clickhouse_client.database}.{table}", list(rows), column_names=list(columns)
    )


def insert_generated_rows(
    clickhouse_client: Client,
    *,
    key_type: str = "UInt64",
    count: int = 3,
    table: str = SOURCE_TABLE,
) -> None:
    """Insert count rows with ids 0..count-1, stamped at SNAPSHOT_BASE."""
    timestamp = SNAPSHOT_BASE.strftime("%Y-%m-%d %H:%M:%S.%f")
    clickhouse_client.command(
        f"insert into {clickhouse_client.database}.{table} "
        f"select to{key_type}(number), concat('value-', toString(number)), "
        f"['af-south', 'eu-west', 'us-east'][toUInt8((number % 3) + 1)], "
        f"toDateTime64('{timestamp}', 9), toInt8(0), toInt64(1) from numbers({count})"
    )


def late_insert_sql(
    database: str,
    table: str = SOURCE_TABLE,
    *,
    key: int = 99,
    payload: str = "late",
    version: int = 1,
    region: str = "af-south",
) -> str:
    """Build the insert LateWriter runs mid-rebuild, stamped with now64(9) so it lands after the
    captured snapshot bound."""
    return (
        f"insert into {database}.{table} "
        "(id, payload, region, _peerdb_synced_at, _peerdb_is_deleted, _peerdb_version) "
        f"values ({key}, '{payload}', '{region}', now64(9), 0, {version})"
    )


def assert_query_log_available(clickhouse_client: Client) -> None:
    """Fail unless system.query_log exists and log_queries is enabled."""
    exists = query_scalar(
        clickhouse_client,
        "select count() from system.tables where database = 'system' and name = 'query_log'",
    )
    assert exists, (
        "system.query_log is unavailable; start ClickHouse with a query_log section in its "
        "config (see scripts/install-clickhouse.sh)"
    )
    log_queries = query_scalar(
        clickhouse_client, "select value from system.settings where name = 'log_queries'"
    )
    assert log_queries == "1", f"log_queries must be 1 to record queries, got {log_queries!r}"


def server_marker(clickhouse_client: Client) -> int:
    """Server timestamp in microseconds, taken before a dbt run."""
    return query_scalar(clickhouse_client, "select toUnixTimestamp64Micro(now64(6))")


def is_capture_noise(query: str) -> bool:
    """Whether a logged statement belongs to the harness, not the dbt run."""
    return (
        "system.query_log" in query
        or "system flush logs" in query
        # dbt-clickhouse probes atomic exchange support on every connection
        or "__dbt_exchange_test" in query
    )


def fetch_executed_queries(clickhouse_client: Client, since: int) -> list[str]:
    """Statements that finished after the marker, from system.query_log.

    SYSTEM FLUSH LOGS forces the query log buffer to disk; without it the rows are flushed on
    flush_interval_milliseconds, which defaults to 7500 ms. QueryStart rows are excluded so each
    executed statement appears once.
    """
    clickhouse_client.command("system flush logs")
    rows = fetch_rows(
        clickhouse_client,
        "select query from system.query_log "
        "where event_time_microseconds >= fromUnixTimestamp64Micro({since:Int64}) "
        "and is_initial_query = 1 "
        "and type in ('QueryFinish', 'ExceptionWhileProcessing', 'ExceptionBeforeStart') "
        "order by event_time_microseconds, query_id",
        {"since": since},
    )
    return [row[0] for row in rows if not is_capture_noise(row[0])]


@dataclass
class ModelRun:
    """One dbt run: the runner result plus captured events and executed statements."""

    result: DbtInvocationResult
    events: list[EventMsg]
    queries: list[str]

    @property
    def success(self) -> bool:
        """Whether the dbt run succeeded."""
        return bool(self.result.runner_result.success)

    def failure_text(self) -> str:
        """Exception and node messages from the run, joined for substring assertions."""
        parts: list[str] = []

        if self.result.runner_result.exception is not None:
            parts.append(str(self.result.runner_result.exception))

        result = self.result.runner_result.result
        if result is not None and hasattr(result, "results"):
            for node_result in result.results:
                message = getattr(node_result, "message", None)
                if message:
                    parts.append(str(message))

        return "\n".join(parts)

    def event_messages(self) -> list[str]:
        """Captured dbt log messages (e.g. JinjaLogInfo `.msg`) in fire order."""
        messages: list[str] = []
        for event in self.events:
            message = getattr(event.data, "msg", None)
            if isinstance(message, str) and message:
                messages.append(message)
        return messages

    def events_matching(self, pattern: str) -> list[str]:
        """Captured event messages matching the given regex."""
        regex = re.compile(pattern)
        return [message for message in self.event_messages() if regex.search(message)]

    def queries_matching(self, pattern: str) -> list[str]:
        """Executed statements matching the given case-insensitive regex."""
        regex = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        return [query for query in self.queries if regex.search(query)]


class LateWriter:
    """Insert a row while a full-refresh bucket query is running.

    Waits for a query matching ``trigger_pattern`` to appear in system.processes, then runs
    ``insert_sql`` (which should stamp the row with a snapshot above the captured bound, e.g. with
    now64(9)). Any failure is re-raised from __exit__ so the test cannot pass silently.
    """

    def __init__(
        self,
        insert_sql: str,
        settings: ClickHouseSettings,
        *,
        trigger_pattern: str = "sleepEachRow",
        timeout: float = 60.0,
        poll_interval: float = 0.02,
    ) -> None:
        self.insert_sql = insert_sql
        self.settings = settings
        self.trigger_pattern = f"%{trigger_pattern}%"
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.inserted = False
        self.error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self._thread = threading.Thread(target=self._run, name="late-writer", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        clickhouse_client = make_client(self.settings)
        try:
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                rows = fetch_rows(
                    clickhouse_client,
                    "select query_id from system.processes "
                    "where query like {pattern:String} "
                    "and query not like '%system.processes%'",
                    {"pattern": self.trigger_pattern},
                )
                if rows:
                    clickhouse_client.command(self.insert_sql)
                    self.inserted = True
                    return
                time.sleep(self.poll_interval)
            raise TimeoutError(
                f"no running query matched {self.trigger_pattern!r} within {self.timeout}s"
            )
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
        finally:
            clickhouse_client.close()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        assert self._thread is not None
        self._thread.join(self.timeout + 10)

        if self._thread.is_alive():
            raise AssertionError("late writer thread did not finish")

        if exc_type is None and self.error is not None:
            raise self.error

        if exc_type is None and not self.inserted:
            raise AssertionError("late writer never inserted a row")

        return False


class BucketedIncrementalTest(IntegrationTest):
    """Base for bucketed_incremental integration tests, cleaning the database per test."""

    @pytest.fixture(autouse=True)
    def clean_database(self, clickhouse_client: Client) -> Iterator[None]:
        """Check the query log is usable and drop every relation before each test."""
        assert_query_log_available(clickhouse_client)
        drop_all_relations(clickhouse_client)
        yield

    def run_model(
        self, dbt: Dbt, model: str, clickhouse_client: Client, *, full_refresh: bool = False
    ) -> ModelRun:
        """Run one model, capturing dbt events and the statements executed during the run."""
        since = server_marker(clickhouse_client)

        result = dbt.run(select=model, full_refresh=full_refresh, capture_events=True)

        queries = fetch_executed_queries(clickhouse_client, since)

        return ModelRun(result=result, events=result.events, queries=queries)

    def create_standard_source(
        self, clickhouse_client: Client, rows: Sequence[Sequence[Any]], **kwargs: Any
    ) -> None:
        """Create the standard source table and insert the given rows into it."""
        create_source(clickhouse_client, **kwargs)
        insert_rows(clickhouse_client, rows)


__all__ = [
    "REGIONS",
    "SNAPSHOT_BASE",
    "SOURCE_COLUMNS",
    "SOURCE_TABLE",
    "BucketedIncrementalTest",
    "Client",
    "LateWriter",
    "ModelRun",
    "base_rows",
    "column_names",
    "create_source",
    "drop_all_relations",
    "expected_base_rows",
    "fetch_rows",
    "insert_generated_rows",
    "insert_rows",
    "late_insert_sql",
    "make_client",
    "query_scalar",
    "region_for",
    "relation_exists",
    "relation_names",
    "snapshot_at",
    "table_engine",
    "table_partition_key",
]
