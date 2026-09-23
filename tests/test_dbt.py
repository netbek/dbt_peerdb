from .conftest import IntegrationTest
from dw_lib.dbt import Dbt


class TestDbt(IntegrationTest):
    def test_dbt_run(self, dbt: Dbt):
        runner_result = dbt.run(exclude="test_table")
        assert runner_result.success is True
