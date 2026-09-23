from .helpers import (
    BucketedIncrementalTest,
    create_source,
    insert_rows,
    relation_exists,
    snapshot_at,
)
from clickhouse_connect.driver.client import Client
from dw_lib.dbt import Dbt

import pytest

CONFIG_ERROR_CASES = [
    (
        "bi_bucket_key_missing",
        "bucket_key_column is required and must be a bare column identifier",
    ),
    (
        "bi_bucket_key_invalid",
        "bucket_key_column is required and must be a bare column identifier",
    ),
    (
        "bi_snapshot_missing",
        "bucket_snapshot_column is required and must be a bare column identifier",
    ),
    (
        "bi_snapshot_invalid",
        "bucket_snapshot_column is required and must be a bare column identifier",
    ),
    (
        "bi_snapshot_equals_key",
        "bucket_snapshot_column must be a different column from bucket_key_column",
    ),
    (
        "bi_source_missing_config",
        'bucket_source_table is required and must have the form "database.table"',
    ),
    (
        "bi_source_invalid_config",
        'bucket_source_table is required and must have the form "database.table"',
    ),
    ("bi_unique_key_missing", 'unique_key must be the single bucket_key_column "id"'),
    ("bi_unique_key_mismatch", 'unique_key must be the single bucket_key_column "id"'),
    ("bi_unique_key_multi", 'unique_key must be the single bucket_key_column "id"'),
    ("bi_unique_key_empty", 'unique_key must be the single bucket_key_column "id"'),
    ("bi_rows_bool", "rows_per_bucket must be a positive integer"),
    ("bi_rows_zero", "rows_per_bucket must be a positive integer"),
    ("bi_rows_float", "rows_per_bucket must be a positive integer"),
    ("bi_rows_negative", "rows_per_bucket must be a positive integer"),
    ("bi_rows_string", "rows_per_bucket must be a positive integer"),
    ("bi_on_concurrent_invalid", 'on_concurrent_writes must be one of "warn", "error", "ignore"'),
    ("bi_inserts_only", "inserts_only is not supported"),
    (
        "bi_strategy_append",
        'only the delete_insert incremental strategy is supported, got "append"',
    ),
    (
        "bi_strategy_legacy",
        'only the delete_insert incremental strategy is supported, got "legacy"',
    ),
]


class TestConfigurationValidation(BucketedIncrementalTest):
    """Config validation.

    Every rule reads config values only and runs before pre-hooks and any database work.
    """

    @pytest.mark.parametrize("model,expected", CONFIG_ERROR_CASES)
    def test_config_error(self, dbt: Dbt, clickhouse_client: Client, model: str, expected: str):
        """Each invalid config value raises a compiler error before any bucket statement runs."""
        run = self.run_model(dbt, model, clickhouse_client)

        assert run.success is False
        assert expected in run.failure_text()
        assert run.queries_matching(r"__dbt_tmp") == []
        assert run.log_lines(r"Processing bucket") == []

    def test_validation_runs_before_hooks(self, dbt: Dbt, clickhouse_client: Client):
        """Invalid config fails before the pre-hook can create its sentinel table."""
        run = self.run_model(dbt, "bi_bad_hook", clickhouse_client)

        assert run.success is False
        assert "rows_per_bucket must be a positive integer" in run.failure_text()
        assert relation_exists(clickhouse_client, "bi_hook_sentinel") is False


class TestMarkerValidation(BucketedIncrementalTest):
    """The marker contract.

    The full-refresh SQL must contain -- __BUCKET_PREDICATE__ exactly once; the materialization
    replaces it with each bucket's predicate.
    """

    def test_marker_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A missing marker stops the run with `found 0`, and the temporary relation is never
        created."""
        run = self.run_model(dbt, "bi_no_marker", clickhouse_client)

        assert run.success is False
        assert (
            "-- __BUCKET_PREDICATE__ must appear exactly once in the model SQL; found 0"
            in run.failure_text()
        )
        assert run.queries_matching(r"__dbt_tmp") == []

    def test_marker_duplicated(self, dbt: Dbt, clickhouse_client: Client):
        """A duplicated marker stops the run with `found 2`, and the temporary relation is never
        created."""
        run = self.run_model(dbt, "bi_two_markers", clickhouse_client)

        assert run.success is False
        assert (
            "-- __BUCKET_PREDICATE__ must appear exactly once in the model SQL; found 2"
            in run.failure_text()
        )
        assert run.queries_matching(r"__dbt_tmp") == []


class TestSourceContract(BucketedIncrementalTest):
    """Source probe.

    bucket_source_table must resolve to a table, and key and snapshot columns are read with
    data_type, so Nullable/LowCardinality wrappers are rejected.
    """

    def test_source_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_source_table that does not exist stops the run, naming the model and table."""
        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_source_table "default.bi_source" not found for model "bi_basic"'
            in run.failure_text()
        )

    def test_source_is_not_a_table(self, dbt: Dbt, clickhouse_client: Client):
        """A view where a table is required stops the run, naming the relation type."""
        clickhouse_client.command("create view default.bi_source as select 1 as id")

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_source_table "default.bi_source" must be a table, got type "view"'
            in run.failure_text()
        )

    def test_key_column_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_key_column absent from the source stops the run."""
        create_source(clickhouse_client, include_key=False)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_key_column "id" not found in bucket_source_table "default.bi_source"'
            in run.failure_text()
        )

    @pytest.mark.parametrize(
        "key_type",
        ["String", "Nullable(UUID)", "LowCardinality(UUID)", "LowCardinality(Nullable(UUID))"],
    )
    def test_key_column_unsupported_type(self, dbt: Dbt, clickhouse_client: Client, key_type: str):
        """String and wrapped keys (Nullable, LowCardinality and LowCardinality(Nullable)) are
        rejected; the probe reads the adapter's nullability flags and reports the wrapper via
        data_type."""
        create_source(clickhouse_client, key_type=key_type)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)
        failure = run.failure_text()

        assert run.success is False
        assert "has unsupported type" in failure
        assert "expected a non-null UUID, signed integer or unsigned integer column" in failure

    def test_snapshot_column_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_snapshot_column absent from the source stops the run."""
        create_source(clickhouse_client, include_snapshot=False)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_snapshot_column "_peerdb_synced_at" not found in bucket_source_table '
            '"default.bi_source"' in run.failure_text()
        )

    @pytest.mark.parametrize(
        "snapshot_type", ["DateTime", "DateTime64(3)", "Nullable(DateTime64(9))"]
    )
    def test_snapshot_column_unsupported_type(
        self, dbt: Dbt, clickhouse_client: Client, snapshot_type: str
    ):
        """DateTime, DateTime64(3) and Nullable(DateTime64(9)) snapshots are rejected; pinning needs
        a non-null DateTime64(9)."""
        create_source(clickhouse_client, snapshot_type=snapshot_type)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert "must be a non-null DateTime64(9) column" in run.failure_text()

    def test_negative_integer_keys(self, dbt: Dbt, clickhouse_client: Client):
        """The count query counts negative integer keys, and the run stops because modulo would
        silently drop them."""
        create_source(clickhouse_client, key_type="Int64")
        insert_rows(clickhouse_client, [(-1, "negative", "us-east", snapshot_at(0), 0, 1)])

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert 'has 1 negative values in "id"' in run.failure_text()
