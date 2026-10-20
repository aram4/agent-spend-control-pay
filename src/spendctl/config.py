from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://spendctl:spendctl@localhost:5432/spendctl"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "spendctl"

    # Real Privacy API calls only happen when this is explicitly true.
    privacy_live: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
