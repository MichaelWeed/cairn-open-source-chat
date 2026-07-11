from app.config import Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.chat_message_max_chars == 500


def test_origins_splits_and_strips() -> None:
    settings = Settings(origin_allowlist=" http://a.example , http://b.example")
    assert settings.origins == ["http://a.example", "http://b.example"]


def test_origins_empty_string_yields_empty_list() -> None:
    settings = Settings(origin_allowlist="")
    assert settings.origins == []
