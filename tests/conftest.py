import os

from spendctl.config import Settings

# Point everything at the test database before get_settings() is first called.
os.environ["DATABASE_URL"] = Settings().test_database_url

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_db(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
