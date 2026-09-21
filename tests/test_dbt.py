from .conftest import InvocationTest
from dw_lib.database import ClickHouseAdapter
from dw_lib.dbt import Dbt


class TestDbt(InvocationTest):
    def test_run(self, clickhouse_adapter: ClickHouseAdapter, dbt: Dbt):
        runner_result = dbt.run(exclude="test_table")
        assert runner_result.success is True
