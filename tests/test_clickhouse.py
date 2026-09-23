from .conftest import IntegrationTest
from clickhouse_connect.driver.client import Client
from dbt.adapters.clickhouse.column import ClickHouseColumn


class TestClickHouse(IntegrationTest):
    """Smoke tests for the ClickHouse server the suite runs against."""

    def test_clickhouse_version(self, clickhouse_client: Client):
        """The server reports the pinned ClickHouse version."""
        result = clickhouse_client.query("select version()")
        assert result.result_rows == [("26.3.33.24",)]

    def test_clickhouse_query_log(self, clickhouse_client: Client):
        """system.query_log exists, since the harness reads executed statements from it after SYSTEM
        FLUSH LOGS."""
        result = clickhouse_client.query("select count() from system.query_log")
        assert result.result_rows is not None

    def test_clickhouse_column_wrapper_flags(self):
        """Dbt-clickhouse strips Nullable/LowCardinality into flags and re-wraps data_type; the
        bucketed_incremental probe relies on both.

        Re-check when dbt-clickhouse is bumped (see integration-test-implementation F1).
        """
        wrapped = ClickHouseColumn("id", "LowCardinality(Nullable(UUID))")
        assert wrapped.is_low_cardinality is True
        assert wrapped.is_nullable is True
        assert wrapped.data_type == "LowCardinality(Nullable(UUID))"

        nullable = ClickHouseColumn("id", "Nullable(UUID)")
        assert nullable.is_low_cardinality is False
        assert nullable.is_nullable is True
        assert nullable.data_type == "Nullable(UUID)"
