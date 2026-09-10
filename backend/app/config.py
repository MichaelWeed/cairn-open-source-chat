import math
from functools import lru_cache
from pathlib import Path
from typing import Annotated, cast

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.api.contracts import OUTPUT_CHARS_MAX, OUTPUT_TOKENS_MAX, SYSTEM_INSTRUCTION_MAX_CHARS

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
    system_instruction: str = Field(default="", max_length=SYSTEM_INSTRUCTION_MAX_CHARS)
    max_output_tokens: int = Field(default=OUTPUT_TOKENS_MAX, ge=1, le=OUTPUT_TOKENS_MAX)
    max_output_chars: int = Field(default=OUTPUT_CHARS_MAX, ge=1, le=OUTPUT_CHARS_MAX)
    database_path: Path = DEFAULT_DB_PATH
    chroma_path: Path = DEFAULT_CHROMA_PATH
    # None preserves the deterministic local/test startup path. The live
    # Compose override sets this to its read-only operator corpus mount.
    corpus_path: Path | None = None
    embedding_model: str = "nomic-embed-text"
    retrieval_top_k: Annotated[int, Field(ge=1, le=6)] = 4
    retrieval_max_distance: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 1.2

    rate_limit_ip_capacity: float = 20
    rate_limit_ip_refill_per_minute: float = 20
    rate_limit_session_capacity: float = 10
    rate_limit_session_refill_per_minute: float = 10

    @field_validator("retrieval_top_k", mode="before")
    @classmethod
    def validate_retrieval_top_k_type(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("retrieval_top_k must be an integer")
        if isinstance(value, str):
            if not value.isascii() or not value.isdecimal():
                raise ValueError("retrieval_top_k must be an integer")
            return int(value)
        if type(value) is not int:
            raise ValueError("retrieval_top_k must be an integer")
        return value

    @field_validator("retrieval_max_distance", mode="before")
    @classmethod
    def validate_retrieval_max_distance_type(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("retrieval_max_distance must be a finite non-negative number")
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError as error:
                raise ValueError(
                    "retrieval_max_distance must be a finite non-negative number"
                ) from error
        if type(value) not in {int, float}:
            raise ValueError("retrieval_max_distance must be a finite non-negative number")
        try:
            finite = math.isfinite(float(cast(int | float, value)))
        except OverflowError as error:
            raise ValueError(
                "retrieval_max_distance must be a finite non-negative number"
            ) from error
        if not finite:
            raise ValueError("retrieval_max_distance must be a finite non-negative number")
        return value

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
