from .conftest import IntegrationTest
from clickhouse_connect.driver.client import Client


class TestClickHouse(IntegrationTest):
    def test_clickhouse_version(self, clickhouse_client: Client):
        result = clickhouse_client.query("select version()")
        assert result.result_rows == [("26.3.33.24",)]

    def test_clickhouse_query_log(self, clickhouse_client: Client):
        result = clickhouse_client.query("select count() from system.query_log")
        assert result.result_rows is not None
