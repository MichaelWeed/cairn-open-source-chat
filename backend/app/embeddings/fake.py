import hashlib
import struct
from typing import Any

from app.embedding_types import EmbeddingInput, EmbeddingVector, EmbeddingVectors

DIMENSIONS = 16


class FakeEmbeddingFunction:
    """Deterministic, offline embedding function without network or model use.

    Hashes each document into a fixed-size vector. Not semantically
    meaningful (don't use for real retrieval quality), only for exercising
    the vector store without needing Ollama running — same role
    `EchoProvider` plays for the chat path.
    """

    def __init__(self) -> None:
        pass

    def __call__(self, input: EmbeddingInput) -> EmbeddingVectors:
        for item in input:
            if not isinstance(item, str):
                raise TypeError(f"FakeEmbeddingFunction only embeds text, got {type(item)}")
        return [self._embed_one(text) for text in input if isinstance(text, str)]

    @staticmethod
    def _embed_one(text: str) -> EmbeddingVector:
        digest = hashlib.sha256(text.encode()).digest()
        floats = struct.unpack(f"{DIMENSIONS}b", digest[:DIMENSIONS])
        return [f / 127.0 for f in floats]

    @staticmethod
    def name() -> str:
        return "fake"

    def get_config(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "FakeEmbeddingFunction":
        return FakeEmbeddingFunction()
