from .conftest import IntegrationTest
from clickhouse_connect.driver.client import Client
from dw_lib.dbt import Dbt


class TestExample(IntegrationTest):
    def test_clickhouse_version(self, clickhouse_client: Client):
        result = clickhouse_client.query("select version()")
        assert result.result_rows == [("26.3.33.24",)]

    def test_clickhouse_query_log(self, clickhouse_client: Client):
        result = clickhouse_client.query("select count() from system.query_log")
        assert result.result_rows is not None

    def test_dbt_run(self, dbt: Dbt):
        runner_result = dbt.run(exclude="test_table")
        assert runner_result.success is True
