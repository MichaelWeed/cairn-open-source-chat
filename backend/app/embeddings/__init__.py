from app.config import Settings
from app.embedding_types import EmbeddingFunction
from app.embeddings.fake import FakeEmbeddingFunction
from app.embeddings.ollama import OllamaEmbeddingFunction

__all__ = ["FakeEmbeddingFunction", "OllamaEmbeddingFunction", "default_embedding_function"]


def default_embedding_function(settings: Settings) -> EmbeddingFunction:
    # Same knob shape and same default-safe reasoning as PROVIDER
    # (app/main.py): EMBEDDING_PROVIDER defaults to `fake` (offline,
    # deterministic — a fresh `docker compose up` never requires a model
    # pull just to boot) with `ollama` opt-in for real embeddings.
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddingFunction(
            base_url=settings.ollama_base_url, model=settings.embedding_model
        )
    if settings.embedding_provider == "fake":
        return FakeEmbeddingFunction()
    raise RuntimeError("Unsupported embedding provider configuration")
