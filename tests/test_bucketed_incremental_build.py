from .helpers import (
    base_rows,
    BucketedIncrementalTest,
    create_source,
    expected_base_rows,
    fetch_rows,
    insert_generated_rows,
    insert_rows,
    query_scalar,
    relation_exists,
    snapshot_at,
    SNAPSHOT_BASE,
    table_engine,
    table_partition_key,
)
from clickhouse_connect.driver.client import Client
from datetime import UTC
from dw_lib.dbt import Dbt
from uuid import UUID

import pytest
import re

INTEGER_KEY_TYPES = [
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "UInt128",
    "UInt256",
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "Int128",
    "Int256",
]


class TestFullRefresh(BucketedIncrementalTest):
    """Bucketed full-refresh builds.

    The materialization counts the source, sizes buckets from rows_per_bucket,
    and replaces the marker in each pass with `<key> % N = i and <snapshot> <=
    S0`, pinning the build to the snapshot maximum captured with the count.
    """

    def test_deduplicates_latest_version_per_key(self, dbt: Dbt, clickhouse_client: Client):
        """A key with two versions collapses to the newest, because each bucket dedupes before
        inserting."""
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert (
            fetch_rows(clickhouse_client, "select id, payload from default.bi_basic order by id")
            == expected_base_rows()
        )

    def test_bucket_sizing_and_snapshot_bounds(self, dbt: Dbt, clickhouse_client: Client):
        """11 rows at rows_per_bucket=3 yield ceil(11/3)=4 passes; each pass carries its modulo
        slice plus the `<= S0` bound, S0 being the source's snapshot maximum."""
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert "bucketed_incremental: row_count=11 bucket_count=4 snapshot_max=" in run.log
        for bucket in range(1, 5):
            assert run.log_lines(rf"Processing bucket {bucket} of 4")

        indices = set()
        bucket_queries = run.queries_matching(
            r"id % 4 = \d+ and _peerdb_synced_at <= toDateTime64\("
        )
        assert bucket_queries
        for query in bucket_queries:
            match = re.search(r"id % 4 = (\d+) and", query)
            assert match is not None
            indices.add(int(match.group(1)))

        assert indices == {0, 1, 2, 3}

    def test_defaults_use_single_bucket_and_detection_query(
        self, dbt: Dbt, clickhouse_client: Client
    ):
        """With defaults one bucket covers the source, and the post-build detection query compares
        the source maximum with S0."""
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_defaults", clickhouse_client)

        assert run.success is True
        assert "bucket_count=1" in run.log
        assert len(run.queries_matching(r"as writes_detected")) == 1

    def test_empty_source_builds_empty_table(self, dbt: Dbt, clickhouse_client: Client):
        """A row count of zero takes the empty-build path: the marker becomes
        `where 1 = 0`, no bucket pass runs, and the intermediate publishes empty."""
        create_source(clickhouse_client)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert query_scalar(clickhouse_client, "select count() from default.bi_basic") == 0
        assert "bucket_count=0" in run.log
        assert run.log_lines(r"Processing bucket") == []
        assert run.queries_matching(r"where 1 = 0")

    def test_empty_source_rebuild_publishes_empty_table(self, dbt: Dbt, clickhouse_client: Client):
        """A rebuild of an emptied source still builds through the intermediate relation and
        publishes it with EXCHANGE TABLES."""
        self.create_standard_source(clickhouse_client, base_rows())
        assert self.run_model(dbt, "bi_basic", clickhouse_client).success is True

        clickhouse_client.command("truncate table default.bi_source")

        run = self.run_model(dbt, "bi_basic", clickhouse_client, full_refresh=True)

        assert run.success is True
        assert query_scalar(clickhouse_client, "select count() from default.bi_basic") == 0
        assert run.queries_matching(r"EXCHANGE TABLES")

    def test_bucket_failure_leaves_target_untouched_and_cleans_up(
        self, dbt: Dbt, clickhouse_client: Client
    ):
        """A failing bucket aborts before the publish step, so the target keeps its previous rows;
        the next run drops the leftover `__dbt_tmp` before rebuilding."""
        clean_rows = [row for row in base_rows() if row[0] != 3]
        self.create_standard_source(clickhouse_client, clean_rows)
        assert self.run_model(dbt, "bi_failing", clickhouse_client).success is True
        before = fetch_rows(
            clickhouse_client, "select id, payload from default.bi_failing order by id"
        )
        assert len(before) == 9

        insert_rows(clickhouse_client, [(3, "poison", snapshot_at(30), 0, 1)])
        run = self.run_model(dbt, "bi_failing", clickhouse_client, full_refresh=True)

        assert run.success is False
        assert "intentional bucket failure" in run.failure_text()
        assert (
            fetch_rows(clickhouse_client, "select id, payload from default.bi_failing order by id")
            == before
        )

        create_source(clickhouse_client)
        insert_rows(clickhouse_client, clean_rows)
        retry = self.run_model(dbt, "bi_failing", clickhouse_client, full_refresh=True)

        assert retry.success is True
        assert relation_exists(clickhouse_client, "bi_failing__dbt_tmp") is False
        assert query_scalar(clickhouse_client, "select count() from default.bi_failing") == 9

    @pytest.mark.parametrize("key_type", INTEGER_KEY_TYPES)
    def test_integer_key_types(self, dbt: Dbt, clickhouse_client: Client, key_type: str):
        """The key type is inferred from the source column; integers bucket by a bare `id % N` and
        every signed and unsigned width builds."""
        create_source(clickhouse_client, key_type=key_type)
        insert_generated_rows(clickhouse_client, key_type=key_type, count=3)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert fetch_rows(clickhouse_client, "select id from default.bi_basic order by id") == [
            (0,),
            (1,),
            (2,),
        ]
        assert run.queries_matching(r"where id % \d+ = \d+")
        assert run.queries_matching(r"reinterpretAsUInt64") == []

    def test_uuid_key(self, dbt: Dbt, clickhouse_client: Client):
        """UUID has no modulo, so the inferred uuid key buckets through reinterpretAsUInt64."""
        create_source(clickhouse_client, key_type="UUID")
        keys = [UUID(int=i) for i in range(3)]
        insert_rows(
            clickhouse_client,
            [(key, f"value-{i}", snapshot_at(i), 0, 1) for i, key in enumerate(keys)],
        )

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert fetch_rows(clickhouse_client, "select id from default.bi_basic order by id") == [
            (key,) for key in keys
        ]
        assert run.queries_matching(r"reinterpretAsUInt64\(id\) % \d+ = \d+")

    def test_snapshot_timezone_is_kept_in_bound(self, dbt: Dbt, clickhouse_client: Client):
        """The bound literal is built from the column dtype, so a DateTime64(9, 'UTC') snapshot
        keeps its timezone."""
        create_source(clickhouse_client, snapshot_type="DateTime64(9, 'UTC')")
        insert_rows(
            clickhouse_client,
            [(0, "value-0", SNAPSHOT_BASE.replace(tzinfo=UTC), 0, 1)],
        )

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert run.queries_matching(r"toDateTime64\('[^']+', 9, 'UTC'\)")

    def test_full_then_incremental_then_full_refresh_paths(
        self, dbt: Dbt, clickhouse_client: Client
    ):
        """is_incremental() is true only when the target is a table and the run is not a full
        refresh, so the first and --full-refresh runs bucket while the middle run uses
        delete+insert."""
        self.create_standard_source(clickhouse_client, base_rows())

        first = self.run_model(dbt, "bi_basic", clickhouse_client)
        assert first.success is True
        assert first.log_lines(r"bucketed_incremental: row_count=")
        assert first.queries_matching(r"__dbt_new_data_") == []

        second = self.run_model(dbt, "bi_basic", clickhouse_client)
        assert second.success is True
        assert second.log_lines(r"bucketed_incremental: row_count=") == []
        assert second.queries_matching(r"__dbt_new_data_")

        third = self.run_model(dbt, "bi_basic", clickhouse_client, full_refresh=True)
        assert third.success is True
        assert third.log_lines(r"bucketed_incremental: row_count=")
        assert third.queries_matching(r"EXCHANGE TABLES")

    def test_package_qualified_incremental_detection(self, dbt: Dbt, clickhouse_client: Client):
        """Models must call dbt_peerdb.is_incremental(): the package macro reports true on an
        incremental run, while the adapter/global plain call never recognises bucketed_incremental.

        Catches a package rename or a dbt resolution change.
        """
        self.create_standard_source(clickhouse_client, base_rows())

        first = self.run_model(dbt, "bi_incremental_detection", clickhouse_client)
        assert first.success is True
        assert first.log_lines(r"bucketed_incremental: row_count=")
        assert (
            query_scalar(
                clickhouse_client,
                "select distinct qualified_incremental from default.bi_incremental_detection",
            )
            == 0
        )
        assert (
            query_scalar(
                clickhouse_client,
                "select distinct plain_incremental from default.bi_incremental_detection",
            )
            == 0
        )

        insert_rows(clickhouse_client, [(100, "value-100", snapshot_at(30), 0, 1)])

        second = self.run_model(dbt, "bi_incremental_detection", clickhouse_client)

        assert second.success is True
        assert second.queries_matching(r"__dbt_new_data_")
        assert fetch_rows(
            clickhouse_client,
            "select id, qualified_incremental, plain_incremental "
            "from default.bi_incremental_detection where id in (0, 100) order by id",
        ) == [(0, 0, 0), (100, 1, 0)]

    def test_partition_by_is_applied_to_the_built_table(self, dbt: Dbt, clickhouse_client: Client):
        """partition_by reaches the adapter's create table as PARTITION BY, so the published target
        is partitioned; adapter strategy validation does not consult partition_by for
        delete_insert."""
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_partitioned", clickhouse_client)

        assert run.success is True
        assert table_engine(clickhouse_client, "bi_partitioned") == "MergeTree"
        assert table_partition_key(clickhouse_client, "bi_partitioned") in ("id", "(id)")
        assert query_scalar(clickhouse_client, "select count() from default.bi_partitioned") == 10
