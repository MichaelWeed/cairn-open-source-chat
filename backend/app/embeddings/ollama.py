from typing import Any

import httpx

from app.embedding_types import EmbeddingInput, EmbeddingVectors


class OllamaEmbeddingFunction:
    """Local-model embedding function (default for real corpora), calling
    Ollama's batch /api/embed endpoint. Synchronous like the vector-store
    interface, unlike the chat path's Provider."""

    # See OllamaProvider's identical default for why httpx's unconfigured
    # 5s is too tight for a local model — a batch of chunks from a large
    # ingested document is the likely case here (task 2.3 will make large
    # batches routine), not a single short query.
    _DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)

    def __init__(self, base_url: str, model: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.Client(timeout=self._DEFAULT_TIMEOUT)

    def __call__(self, input: EmbeddingInput) -> EmbeddingVectors:
        for item in input:
            if not isinstance(item, str):
                raise TypeError(f"OllamaEmbeddingFunction only embeds text, got {type(item)}")
        response = self._client.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": list(input)},
        )
        response.raise_for_status()
        embeddings = response.json()["embeddings"]
        return [[float(value) for value in embedding] for embedding in embeddings]

    @staticmethod
    def name() -> str:
        return "ollama"

    def get_config(self) -> dict[str, Any]:
        return {"base_url": self._base_url, "model": self._model}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "OllamaEmbeddingFunction":
        return OllamaEmbeddingFunction(base_url=config["base_url"], model=config["model"])
