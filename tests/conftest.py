from clickhouse_connect.driver.client import Client
from collections.abc import Generator
from dw_lib.database import ClickHouseAdapter, ClickHouseSettings
from dw_lib.dbt import Dbt
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, RootModel
from ruamel.yaml import YAML
from typing import Any, Literal

import pytest
import time
import urllib.request


class DbtTargetSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["clickhouse"]
    threads: int
    host: str
    port: int
    user: str
    password: str
    schema_: str = Field(alias="schema")
    driver: Literal["http", "native"]
    secure: bool = False
    use_lw_deletes: bool = True


class DbtProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: str
    outputs: dict[str, DbtTargetSettings]


class DbtProfiles(RootModel[dict[str, DbtProfile]]):
    pass


def to_clickhouse_settings(dbt_target_settings: DbtTargetSettings) -> ClickHouseSettings:
    return ClickHouseSettings(
        host=dbt_target_settings.host,
        port=dbt_target_settings.port,
        username=dbt_target_settings.user,
        password=dbt_target_settings.password,
        database=dbt_target_settings.schema_,
    )


class IntegrationTest:
    @pytest.fixture(scope="session")
    def clickhouse_settings(self, dbt_target_settings: DbtTargetSettings) -> ClickHouseSettings:
        return to_clickhouse_settings(dbt_target_settings)

    @pytest.fixture(scope="session")
    def clickhouse_adapter(
        self, clickhouse_settings: ClickHouseSettings
    ) -> Generator[ClickHouseAdapter, Any]:
        timeout = 10
        pause = 1
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                url = f"http://{clickhouse_settings.host}:{clickhouse_settings.port}/ping"
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:  # noqa: BLE001, S110
                pass
            time.sleep(pause)
        else:
            raise TimeoutError("Timeout reached while waiting for ClickHouse server")

        clickhouse_adapter = ClickHouseAdapter(clickhouse_settings)

        with clickhouse_adapter.create_client() as clickhouse_client:
            while time.monotonic() < deadline:
                try:
                    exists = clickhouse_client.query(
                        "select count() from system.tables "
                        "where database = 'system' and name = 'query_log'"
                    ).first_row[0]
                    if exists:
                        log_queries = clickhouse_client.query(
                            "select value from system.settings where name = 'log_queries'"
                        ).first_row[0]
                        if str(log_queries) == "1":
                            break
                except Exception:  # noqa: BLE001, S110
                    pass
                time.sleep(pause)
            else:
                raise TimeoutError("Timeout reached while waiting for ClickHouse system.query_log")

        yield clickhouse_adapter

    @pytest.fixture(scope="session")
    def clickhouse_client(self, clickhouse_adapter: ClickHouseAdapter) -> Generator[Client, Any]:
        with clickhouse_adapter.create_client() as clickhouse_client:
            yield clickhouse_client

    @pytest.fixture(scope="session")
    def dbt_profiles_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / ".dbt"

    @pytest.fixture(scope="session")
    def dbt_project_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / "dbt"

    @pytest.fixture(scope="session")
    def dbt_target_settings(self, dbt_profiles_dir: Path) -> DbtTargetSettings:
        yaml = YAML(typ="safe")
        with open(dbt_profiles_dir / "profiles.yml") as fp:
            data = yaml.load(fp)
        dbt_profiles = DbtProfiles.model_validate(data)
        return dbt_profiles.root["example"].outputs["dev"]

    @pytest.fixture(scope="session")
    def dbt(self, dbt_profiles_dir: Path, dbt_project_dir: Path) -> Dbt:
        return Dbt(profiles_dir=dbt_profiles_dir, project_dir=dbt_project_dir, target="dev")
