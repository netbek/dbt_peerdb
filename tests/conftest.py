from clickhouse_connect.driver.client import Client
from collections.abc import Generator
from dw_lib.database import ClickHouseAdapter, ClickHouseSettings
from dw_lib.dbt import Dbt
from pathlib import Path
from pydantic import BaseModel, Field
from ruamel.yaml import YAML
from typing import Any, Literal

import pytest


class DbtTargetSettings(BaseModel):
    driver: Literal["http", "native"]
    host: str
    port: int
    username: str = Field(alias="user")
    password: str
    database: str = Field(alias="schema")


def to_clickhouse_settings(dbt_target_settings: DbtTargetSettings) -> ClickHouseSettings:
    return ClickHouseSettings(
        host=dbt_target_settings.host,
        port=dbt_target_settings.port,
        username=dbt_target_settings.username,
        password=dbt_target_settings.password,
        database=dbt_target_settings.database,
    )


class IntegrationTest:
    @pytest.fixture(scope="session")
    def clickhouse_settings(self, dbt_profiles: dict) -> Generator[ClickHouseSettings, Any]:
        dbt_target_settings = DbtTargetSettings(**dbt_profiles["example"]["outputs"]["dev"])
        yield to_clickhouse_settings(dbt_target_settings)

    @pytest.fixture(scope="session")
    def clickhouse_adapter(
        self, clickhouse_settings: ClickHouseSettings
    ) -> Generator[ClickHouseAdapter, Any]:
        yield ClickHouseAdapter(clickhouse_settings)

    @pytest.fixture(scope="session")
    def clickhouse_client(self, clickhouse_adapter: ClickHouseAdapter) -> Generator[Client, Any]:
        with clickhouse_adapter.create_client() as client:
            yield client

    @pytest.fixture(scope="session")
    def dbt_profiles_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / ".dbt"

    @pytest.fixture(scope="session")
    def dbt_project_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / "dbt"

    @pytest.fixture(scope="session")
    def dbt_profiles(self, dbt_profiles_dir: Path) -> dict:
        yaml = YAML(typ="safe")
        with open(dbt_profiles_dir / "profiles.yml") as fp:
            data = yaml.load(fp)
        return data

    @pytest.fixture(scope="session")
    def dbt(self, dbt_profiles_dir: Path, dbt_project_dir: Path) -> Generator[Dbt, Any]:
        yield Dbt(profiles_dir=dbt_profiles_dir, project_dir=dbt_project_dir, target="dev")
