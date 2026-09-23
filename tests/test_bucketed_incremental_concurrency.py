from .helpers import (
    base_rows,
    BucketedIncrementalTest,
    fetch_rows,
    late_insert_sql,
    LateWriter,
    query_scalar,
    region_for,
    snapshot_at,
)
from clickhouse_connect.driver.client import Client
from dw_lib.database import ClickHouseSettings
from dw_lib.dbt import Dbt

CONCURRENT_ROWS = [(i, f"value-{i}", region_for(i), snapshot_at(i), 0, 1) for i in range(6)]


class TestConcurrentWrites(BucketedIncrementalTest):
    """on_concurrent_writes modes.

    Every bucket is pinned to S0, so a write landing mid-build is excluded; after the loop the
    materialization compares the source maximum with S0 and fails closed by default.
    """

    def test_error_mode_fails_and_leaves_target_untouched(
        self, dbt: Dbt, clickhouse_client: Client, clickhouse_settings: ClickHouseSettings
    ):
        """A write stamped after S0 stops the run before the publish step, leaving the previous
        table in place."""
        self.create_standard_source(clickhouse_client, CONCURRENT_ROWS)
        assert self.run_model(dbt, "bi_concurrent", clickhouse_client).success is True
        before = fetch_rows(
            clickhouse_client, "select id, payload from default.bi_concurrent order by id"
        )

        with LateWriter(late_insert_sql(clickhouse_settings.database), clickhouse_settings):
            run = self.run_model(dbt, "bi_concurrent", clickhouse_client, full_refresh=True)

        assert run.success is False
        failure = run.failure_text()
        assert "concurrent writes to bucket_source_table" in failure
        assert "previous table was left untouched" in failure
        assert len(run.queries_matching(r"as writes_detected")) == 1
        assert (
            fetch_rows(
                clickhouse_client, "select id, payload from default.bi_concurrent order by id"
            )
            == before
        )

    def test_warn_mode_publishes_and_incremental_converges(
        self, dbt: Dbt, clickhouse_client: Client, clickhouse_settings: ClickHouseSettings
    ):
        """Warn publishes the S0-pinned build without the late row; the next incremental run re-
        selects the key through the >= watermark."""
        self.create_standard_source(clickhouse_client, CONCURRENT_ROWS)
        assert self.run_model(dbt, "bi_concurrent_warn", clickhouse_client).success is True

        with LateWriter(late_insert_sql(clickhouse_settings.database), clickhouse_settings):
            run = self.run_model(dbt, "bi_concurrent_warn", clickhouse_client, full_refresh=True)

        assert run.success is True
        assert run.log_lines(r"WARNING: concurrent writes to bucket_source_table")
        assert len(run.queries_matching(r"as writes_detected")) == 1
        assert (
            query_scalar(
                clickhouse_client, "select count() from default.bi_concurrent_warn where id = 99"
            )
            == 0
        )

        converge = self.run_model(dbt, "bi_concurrent_warn", clickhouse_client)

        assert converge.success is True
        assert (
            query_scalar(
                clickhouse_client, "select count() from default.bi_concurrent_warn where id = 99"
            )
            == 1
        )
        assert (
            query_scalar(
                clickhouse_client, "select payload from default.bi_concurrent_warn where id = 99"
            )
            == "late"
        )

    def test_ignore_mode_runs_no_detection_query(self, dbt: Dbt, clickhouse_client: Client):
        """Ignore skips the max(snapshot) > S0 check entirely and accepts silent divergence."""
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_ignore", clickhouse_client)

        assert run.success is True
        assert run.queries_matching(r"as writes_detected") == []
