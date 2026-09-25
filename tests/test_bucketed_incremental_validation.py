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
        "bi_relation_missing_config",
        "set exactly one of bucket_ref or bucket_source, but neither was set",
    ),
    (
        "bi_relation_both_config",
        "set exactly one of bucket_ref or bucket_source, but both were set",
    ),
    (
        "bi_relation_invalid_config",
        "bucket_ref must be a list or tuple of one or two bare identifiers",
    ),
    ("bi_ref_empty", "bucket_ref must be a list or tuple of one or two bare identifiers"),
    ("bi_ref_three", "bucket_ref must be a list or tuple of one or two bare identifiers"),
    ("bi_ref_bad_element", "bucket_ref must be a list or tuple of one or two bare identifiers"),
    ("bi_source_empty", "bucket_source must be a list or tuple of exactly two bare identifiers"),
    ("bi_source_one", "bucket_source must be a list or tuple of exactly two bare identifiers"),
    ("bi_source_three", "bucket_source must be a list or tuple of exactly two bare identifiers"),
    ("bi_source_nonlist", "bucket_source must be a list or tuple of exactly two bare identifiers"),
    (
        "bi_source_bad_element",
        "bucket_source must be a list or tuple of exactly two bare identifiers",
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
        assert run.events_matching(r"Processing bucket") == []

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

    The resolved bucket relation must resolve to a table, and key and snapshot columns are read with
    data_type, so Nullable/LowCardinality wrappers are rejected.
    """

    def test_source_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_source relation that does not exist stops the run, naming the model and
        relation."""
        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_source "default.bi_source" not found for model "bi_basic"' in run.failure_text()
        )

    def test_source_is_not_a_table(self, dbt: Dbt, clickhouse_client: Client):
        """A view where a table is required stops the run, naming the relation type."""
        clickhouse_client.command("create view default.bi_source as select 1 as id")

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_source "default.bi_source" must be a table, got type "view"'
            in run.failure_text()
        )

    def test_key_column_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_key_column absent from the relation stops the run."""
        create_source(clickhouse_client, include_key=False)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_key_column "id" not found in bucket_source "default.bi_source"'
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
        """A bucket_snapshot_column absent from the relation stops the run."""
        create_source(clickhouse_client, include_snapshot=False)

        run = self.run_model(dbt, "bi_basic", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_snapshot_column "_peerdb_synced_at" not found in bucket_source '
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


class TestRefBranchContract(BucketedIncrementalTest):
    """The ref branch resolves through ref() and shares the same relation contract."""

    def test_ref_relation_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_ref target that does not exist stops the run, naming the ref relation."""
        run = self.run_model(dbt, "bi_ref", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_ref "default.bi_ref_source" not found for model "bi_ref"' in run.failure_text()
        )

    def test_ref_relation_is_not_a_table(self, dbt: Dbt, clickhouse_client: Client):
        """A view where the ref target should be a table stops the run."""
        clickhouse_client.command("create view default.bi_ref_source as select 1 as id")

        run = self.run_model(dbt, "bi_ref", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_ref "default.bi_ref_source" must be a table, got type "view"'
            in run.failure_text()
        )

    def test_ref_key_column_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_key_column absent from the ref relation stops the run."""
        create_source(clickhouse_client, table="bi_ref_source", include_key=False)

        run = self.run_model(dbt, "bi_ref", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_key_column "id" not found in bucket_ref "default.bi_ref_source"'
            in run.failure_text()
        )

    def test_ref_snapshot_column_missing(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_snapshot_column absent from the ref relation stops the run."""
        create_source(clickhouse_client, table="bi_ref_source", include_snapshot=False)

        run = self.run_model(dbt, "bi_ref", clickhouse_client)

        assert run.success is False
        assert (
            'bucket_snapshot_column "_peerdb_synced_at" not found in bucket_ref '
            '"default.bi_ref_source"' in run.failure_text()
        )


class TestResolutionAndTiming(BucketedIncrementalTest):
    """Resolution failures surface as dbt errors; incremental runs skip resolution."""

    def test_unknown_ref_stops_run(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_ref naming a model that is not in the manifest stops the run with dbt's
        resolution error."""
        run = self.run_model(dbt, "bi_ref_unknown", clickhouse_client)

        assert run.success is False
        assert "bi_not_a_model" in run.failure_text()

    def test_unknown_source_stops_run(self, dbt: Dbt, clickhouse_client: Client):
        """A bucket_source naming an undeclared source stops the run with dbt's resolution error."""
        run = self.run_model(dbt, "bi_source_unknown", clickhouse_client)

        assert run.success is False
        assert "bi_not_declared" in run.failure_text()

    def test_incremental_run_skips_resolution(self, dbt: Dbt, clickhouse_client: Client):
        """An incremental run never resolves bucket_ref: if it did, the unknown ref target would
        stop it.

        The existing target was created outside dbt.
        """
        clickhouse_client.command(
            "create table default.bi_ref_unknown (id UInt64) engine MergeTree order by tuple()"
        )

        run = self.run_model(dbt, "bi_ref_unknown", clickhouse_client)

        assert run.success is True
        assert run.events_matching(r"row_count=") == []

    def test_incremental_run_rejects_bad_shape(self, dbt: Dbt, clickhouse_client: Client):
        """Shape validation runs on incremental runs too: with an existing target the run is
        incremental, yet the wrong-shaped key still stops it before any bucket work."""
        clickhouse_client.command(
            "create table default.bi_ref_bad_element (id UInt64) engine MergeTree order by tuple()"
        )

        run = self.run_model(dbt, "bi_ref_bad_element", clickhouse_client)

        assert run.success is False
        assert (
            "bucket_ref must be a list or tuple of one or two bare identifiers"
            in run.failure_text()
        )
        assert run.queries_matching(r"__dbt_tmp") == []
