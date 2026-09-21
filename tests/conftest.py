from collections.abc import Generator
from dw_lib.database import ClickHouseAdapter, ClickHouseSettings
from pathlib import Path
from typing import Any

import pytest


class DatabaseTest:
    @pytest.fixture(scope="module")
    def docker_compose_file(self) -> Path:
        return Path(__file__).parent / "docker-compose.yml"

    @pytest.fixture(scope="module")
    def docker_compose_project_name(self) -> str:
        return "dbt-peerdb-test-database"  # Pin the project name to avoid creating multiple stacks

    # @pytest.fixture(scope="module")
    # def docker_setup(self) -> list[str] | str:
    #     return ["down -v", "up --build --wait"]  # Stop the stack before starting a new one

    @pytest.fixture(scope="module")
    def clickhouse_settings(self) -> ClickHouseSettings:
        return ClickHouseSettings(
            host="localhost",
            port=18123,
            username="default",
            password="default",
            database="default",
            driver="connect",
        )

    @pytest.fixture(scope="module")
    def clickhouse_adapter(
        self, docker_services, clickhouse_settings: ClickHouseSettings
    ) -> Generator[ClickHouseAdapter, Any]:
        clickhouse_adapter = ClickHouseAdapter(clickhouse_settings)

        def is_responsive():
            try:
                with clickhouse_adapter.create_client() as client:
                    client.query("select 1;")
                return True
            except Exception:  # noqa: BLE001
                return False

        docker_services.wait_until_responsive(check=is_responsive, timeout=10, pause=1)

        yield clickhouse_adapter
