from collections.abc import Generator
from dw_lib.database import ClickHouseAdapter, ClickHouseSettings
from dw_lib.dbt import Dbt
from pathlib import Path
from typing import Any

import pytest
import urllib.error
import urllib.request


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
            url = f"http://{clickhouse_settings.host}:{clickhouse_settings.port}/ping"
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    return response.status == 200
            except (urllib.error.URLError, TimeoutError, OSError):
                return False

        docker_services.wait_until_responsive(check=is_responsive, timeout=10, pause=1)

        yield clickhouse_adapter


class IntegrationTest(DatabaseTest):
    @pytest.fixture
    def profiles_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / ".dbt"

    @pytest.fixture
    def project_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / "dbt"

    @pytest.fixture
    def dbt(
        self, profiles_dir: Path, project_dir: Path, clickhouse_adapter: ClickHouseAdapter
    ) -> Dbt:
        return Dbt(profiles_dir=profiles_dir, project_dir=project_dir)
