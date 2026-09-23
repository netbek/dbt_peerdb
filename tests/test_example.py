from .conftest import IntegrationTest
from clickhouse_connect.driver.client import Client
from dw_lib.dbt import Dbt


class TestExample(IntegrationTest):
    def test_clickhouse_query(self, clickhouse_client: Client):
        result = clickhouse_client.query("select count() from system.query_log")
        print(result.result_rows)

    def test_dbt_run(self, dbt: Dbt):
        runner_result = dbt.run(exclude="test_table")
        assert runner_result.success is True
