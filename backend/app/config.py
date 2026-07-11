from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DB_PATH = Path("data/cairn.db")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b-instruct"
    admin_bootstrap_password: str = ""
    origin_allowlist: str = "http://localhost:8080"
    chat_message_max_chars: int = 500
    database_path: Path = DEFAULT_DB_PATH

    @property
    def origins(self) -> list[str]:
        return [origin.strip() for origin in self.origin_allowlist.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
