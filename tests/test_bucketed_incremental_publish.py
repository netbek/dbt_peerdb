from .helpers import (
    base_rows,
    BucketedIncrementalTest,
    expected_base_rows,
    fetch_rows,
    insert_rows,
    query_scalar,
    relation_exists,
    snapshot_at,
    table_engine,
)
from clickhouse_connect.driver.client import Client
from dw_lib.dbt import Dbt


class TestPublication(BucketedIncrementalTest):
    """Publish step.

    A build loads into `<identifier>__dbt_tmp` and publishes in one step: rename on the first run,
    EXCHANGE TABLES when can_exchange, or two renames otherwise.
    """

    def test_first_run_renames_intermediate_into_place(self, dbt: Dbt, clickhouse_client: Client):
        """With no existing relation the intermediate is renamed to the target, with no exchange."""
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert table_engine(clickhouse_client, "bi_basic") == "MergeTree"
        assert relation_exists(clickhouse_client, "bi_basic__dbt_tmp") is False
        assert relation_exists(clickhouse_client, "bi_basic__dbt_backup") is False
        assert run.queries_matching(r"RENAME TABLE")
        assert run.queries_matching(r"__dbt_tmp")
        assert run.queries_matching(r"EXCHANGE TABLES") == []

    def test_rebuild_exchanges_and_drops_backup(self, dbt: Dbt, clickhouse_client: Client):
        """With an existing table the intermediate is renamed to the backup and EXCHANGE TABLES
        swaps it atomically; the backup is dropped after the commit."""
        self.create_standard_source(clickhouse_client, base_rows())
        assert self.run_model(dbt, "bi_basic", clickhouse_client).success is True

        insert_rows(clickhouse_client, [(100, "value-100", "eu-west", snapshot_at(30), 0, 1)])

        run = self.run_model(dbt, "bi_basic", clickhouse_client, full_refresh=True)

        assert run.success is True
        exchanges = run.queries_matching(r"EXCHANGE TABLES")
        assert len(exchanges) == 1
        assert "bi_basic__dbt_backup" in exchanges[0]
        assert relation_exists(clickhouse_client, "bi_basic__dbt_backup") is False
        assert (
            query_scalar(clickhouse_client, "select count() from default.bi_basic where id = 100")
            == 1
        )

    def test_view_target_uses_two_renames(self, dbt: Dbt, clickhouse_client: Client):
        """A view target does not report can_exchange, so the target is renamed to the backup and
        the intermediate to the target."""
        clickhouse_client.command(
            "create view default.bi_basic as "
            "select toUInt64(0) as id, '' as payload, now64(9) as _peerdb_synced_at, "
            "toInt8(0) as _peerdb_is_deleted, toInt64(0) as _peerdb_version"
        )
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert table_engine(clickhouse_client, "bi_basic") == "MergeTree"
        assert len(run.queries_matching(r"RENAME TABLE")) >= 2
        assert run.queries_matching(r"EXCHANGE TABLES") == []
        assert relation_exists(clickhouse_client, "bi_basic__dbt_backup") is False
        assert (
            fetch_rows(clickhouse_client, "select id, payload from default.bi_basic order by id")
            == expected_base_rows()
        )

    def test_preexisting_tmp_and_backup_are_dropped(self, dbt: Dbt, clickhouse_client: Client):
        """Leftovers from an earlier failed run are dropped before the build starts."""
        clickhouse_client.command(
            "create table default.bi_basic__dbt_tmp (junk UInt8) engine Memory"
        )
        clickhouse_client.command(
            "create table default.bi_basic__dbt_backup (junk UInt8) engine Memory"
        )
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is True
        assert (
            fetch_rows(clickhouse_client, "select id, payload from default.bi_basic order by id")
            == expected_base_rows()
        )
        assert relation_exists(clickhouse_client, "bi_basic__dbt_tmp") is False
        assert relation_exists(clickhouse_client, "bi_basic__dbt_backup") is False

    def test_pre_and_post_hooks_run(self, dbt: Dbt, clickhouse_client: Client):
        """Both hooks execute around the build: the pre-hook before the buckets and the post-hook
        after the publish, inside the transaction."""
        clickhouse_client.command(
            "create table default.bi_hook_log (event String) engine MergeTree order by event"
        )
        self.create_standard_source(clickhouse_client, base_rows())

        run = self.run_model(dbt, "bi_hooks", clickhouse_client)

        assert run.success is True
        assert fetch_rows(
            clickhouse_client, "select event from default.bi_hook_log order by event"
        ) == [("post",), ("pre",)]
