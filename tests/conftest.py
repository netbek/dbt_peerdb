from dw_lib.dbt import Dbt
from pathlib import Path

import pytest


class IntegrationTest:
    @pytest.fixture
    def profiles_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / ".dbt"

    @pytest.fixture
    def project_dir(self) -> Path:
        return Path(__file__).parent / "fixtures" / "dbt"

    @pytest.fixture
    def dbt(self, profiles_dir: Path, project_dir: Path) -> Dbt:
        return Dbt(profiles_dir=profiles_dir, project_dir=project_dir)
