from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DB_PATH = Path("data/cairn.db")
DEFAULT_CHROMA_PATH = Path("data/chroma")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b-instruct"
    admin_bootstrap_password: str = ""
    # Host port the stack is published on (compose maps it to the
    # container's 8000). Only used to derive the default origin allowlist.
    cairn_port: int = 8080
    origin_allowlist: str = ""
    chat_message_max_chars: int = 500
    database_path: Path = DEFAULT_DB_PATH
    chroma_path: Path = DEFAULT_CHROMA_PATH
    embedding_model: str = "nomic-embed-text"
    retrieval_top_k: int = 4
    retrieval_max_distance: float = 1.2

    rate_limit_ip_capacity: float = 20
    rate_limit_ip_refill_per_minute: float = 20
    rate_limit_session_capacity: float = 10
    rate_limit_session_refill_per_minute: float = 10

    @property
    def origins(self) -> list[str]:
        # Empty allowlist means "the app's own published origin": the demo
        # and admin pages are served same-origin, and the chat endpoint
        # rejects any Origin outside this list — so the default has to
        # track cairn_port or a port override silently breaks the demo.
        if not self.origin_allowlist.strip():
            return [f"http://localhost:{self.cairn_port}"]
        return [origin.strip() for origin in self.origin_allowlist.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
