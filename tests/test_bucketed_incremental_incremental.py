from .helpers import (
    base_rows,
    BucketedIncrementalTest,
    column_names,
    create_source,
    expected_base_rows,
    fetch_rows,
    insert_rows,
    query_scalar,
    relation_names,
    snapshot_at,
)
from clickhouse_connect.driver.client import Client
from dw_lib.dbt import Dbt


class TestIncrementalMaintenance(BucketedIncrementalTest):
    """Incremental path.

    The adapter builds `<identifier>__dbt_new_data_<invocation_id>` from the model
    SQL, deletes target rows whose unique_key appears there, inserts the temporary
    rows, and drops the table.
    """

    def test_delete_insert_replaces_only_touched_keys(self, dbt: Dbt, clickhouse_client: Client):
        """Only keys in the new batch are deleted and re-inserted; untouched keys
        keep their existing rows."""
        self.create_standard_source(clickhouse_client, base_rows())
        assert self.run_model(dbt, "bi_basic", clickhouse_client).success is True

        insert_rows(
            clickhouse_client,
            [
                (0, "value-0-v2", snapshot_at(30), 0, 2),
                (100, "value-100", snapshot_at(30), 0, 1),
            ],
        )

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        rows = dict(fetch_rows(clickhouse_client, "select id, payload from default.bi_basic"))
        assert rows[0] == "value-0-v2"
        assert rows[1] == "value-1-v2"
        assert rows[5] == "value-5"
        assert rows[100] == "value-100"
        assert len(rows) == 11

        assert run.queries_matching(r"__dbt_new_data_")
        assert run.queries_matching(r"delete from")
        assert run.queries_matching(r"insert into")
        assert run.log_lines(r"Processing bucket") == []
        assert [
            name
            for name in relation_names(clickhouse_client)
            if name.startswith("bi_basic__dbt_new_data")
        ] == []

    def test_predicates_are_passed_to_delete_and_insert(self, dbt: Dbt, clickhouse_client: Client):
        """Configured predicates narrow the delete (`and id >= 0`); the insert is not
        filtered, so predicate semantics are the adapter's."""
        self.create_standard_source(clickhouse_client, base_rows())
        assert self.run_model(dbt, "bi_predicates", clickhouse_client).success is True

        insert_rows(clickhouse_client, [(0, "value-0-v2", snapshot_at(30), 0, 2)])

        run = self.run_model(dbt, "bi_predicates", clickhouse_client)

        assert run.success is True
        assert run.queries_matching(r"delete from .* and id >= 0")
        assert (
            query_scalar(
                clickhouse_client, "select payload from default.bi_predicates where id = 0"
            )
            == "value-0-v2"
        )

    def test_schema_append_new_columns(self, dbt: Dbt, clickhouse_client: Client):
        """With append_new_columns the column additions are applied before the
        delete+insert, so the new column receives data."""
        clickhouse_client.command(
            "create table default.bi_schema_append "
            "(id UInt64, _peerdb_synced_at DateTime64(9), _peerdb_is_deleted Int8, "
            "_peerdb_version Int64) engine MergeTree order by id"
        )
        clickhouse_client.command(
            "insert into default.bi_schema_append values "
            "(7, toDateTime64('2023-01-01 00:00:00.000000000', 9), toInt8(0), toInt64(1))"
        )
        create_source(clickhouse_client)
        insert_rows(clickhouse_client, [(7, "value-7", snapshot_at(30), 0, 2)])

        run = self.run_model(dbt, "bi_schema_append", clickhouse_client)

        assert run.success is True
        assert "payload" in column_names(clickhouse_client, "bi_schema_append")
        assert (
            query_scalar(
                clickhouse_client, "select payload from default.bi_schema_append where id = 7"
            )
            == "value-7"
        )

    def test_schema_sync_all_columns(self, dbt: Dbt, clickhouse_client: Client):
        """With sync_all_columns the obsolete column is dropped and the missing one
        added before the delete+insert."""
        clickhouse_client.command(
            "create table default.bi_schema_sync "
            "(id UInt64, payload String, obsolete String, _peerdb_synced_at DateTime64(9), "
            "_peerdb_is_deleted Int8, _peerdb_version Int64) engine MergeTree order by id"
        )
        clickhouse_client.command(
            "insert into default.bi_schema_sync values "
            "(7, 'old', 'junk', toDateTime64('2023-01-01 00:00:00.000000000', 9), 0, 1)"
        )
        create_source(clickhouse_client)
        insert_rows(clickhouse_client, [(7, "value-7", snapshot_at(30), 0, 2)])

        run = self.run_model(dbt, "bi_schema_sync", clickhouse_client)

        assert run.success is True
        columns = column_names(clickhouse_client, "bi_schema_sync")
        assert "obsolete" not in columns
        assert "payload" in columns
        assert (
            query_scalar(
                clickhouse_client, "select payload from default.bi_schema_sync where id = 7"
            )
            == "value-7"
        )

    def test_schema_fail_stops_the_run(self, dbt: Dbt, clickhouse_client: Client):
        """With on_schema_change='fail' a target/source mismatch stops the run before
        the delete+insert."""
        clickhouse_client.command(
            "create table default.bi_schema_fail "
            "(id UInt64, payload String, obsolete String, _peerdb_synced_at DateTime64(9), "
            "_peerdb_is_deleted Int8, _peerdb_version Int64) engine MergeTree order by id"
        )
        create_source(clickhouse_client)
        insert_rows(clickhouse_client, [(7, "value-7", snapshot_at(30), 0, 2)])

        run = self.run_model(dbt, "bi_schema_fail", clickhouse_client)

        assert run.success is False
        assert "out of sync" in run.failure_text()

    def test_tie_safe_watermark_recaptures_bound_tie(self, dbt: Dbt, clickhouse_client: Client):
        """A version stamped exactly at the previous bound is re-read because the
        model compares with >=; ties are reachable at now64() millisecond
        resolution."""
        self.create_standard_source(clickhouse_client, base_rows())
        assert self.run_model(dbt, "bi_basic", clickhouse_client).success is True

        insert_rows(clickhouse_client, [(1, "value-1-v3", snapshot_at(20), 0, 3)])

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert (
            query_scalar(clickhouse_client, "select payload from default.bi_basic where id = 1")
            == "value-1-v3"
        )

    def test_strict_watermark_misses_bound_tie(self, dbt: Dbt, clickhouse_client: Client):
        """A > watermark cannot re-select a version stamped exactly at the bound, so
        the change stays missed — the documented price of the strict contract."""
        self.create_standard_source(clickhouse_client, base_rows())
        assert self.run_model(dbt, "bi_strict", clickhouse_client).success is True

        insert_rows(clickhouse_client, [(1, "value-1-v3", snapshot_at(20), 0, 3)])

        run = self.run_model(dbt, "bi_strict", clickhouse_client)

        assert run.success is True
        assert (
            fetch_rows(clickhouse_client, "select id, payload from default.bi_strict order by id")
            == expected_base_rows()
        )

    def test_unique_key_list_form_is_accepted(self, dbt: Dbt, clickhouse_client: Client):
        """A list unique_key is normalised to one column and must equal
        bucket_key_column."""
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_unique_key_list", clickhouse_client)

        assert run.success is True
        assert (
            fetch_rows(
                clickhouse_client, "select id, payload from default.bi_unique_key_list order by id"
            )
            == expected_base_rows()
        )
