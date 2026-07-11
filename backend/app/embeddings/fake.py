import hashlib
import struct
from typing import Any

import numpy as np
from chromadb.api.types import Embeddable, Embedding, Embeddings
from chromadb.api.types import EmbeddingFunction as ChromaEmbeddingFunction

DIMENSIONS = 16


class FakeEmbeddingFunction(ChromaEmbeddingFunction[Embeddable]):
    """Deterministic, offline embedding function — no network, no model.

    Hashes each document into a fixed-size vector. Not semantically
    meaningful (don't use for real retrieval quality), only for exercising
    the vector store without needing Ollama running — same role
    `EchoProvider` plays for the chat path.
    """

    def __init__(self) -> None:
        pass

    def __call__(self, input: Embeddable) -> Embeddings:
        # Only text documents are meaningful to hash; the wider Embeddable
        # union (pre-embedded arrays) isn't a real input in this project.
        for item in input:
            if not isinstance(item, str):
                raise TypeError(f"FakeEmbeddingFunction only embeds text, got {type(item)}")
        return [self._embed_one(text) for text in input if isinstance(text, str)]

    @staticmethod
    def _embed_one(text: str) -> Embedding:
        digest = hashlib.sha256(text.encode()).digest()
        floats = struct.unpack(f"{DIMENSIONS}b", digest[:DIMENSIONS])
        return np.array([f / 127.0 for f in floats], dtype=np.float32)

    @staticmethod
    def name() -> str:
        return "fake"

    def get_config(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "FakeEmbeddingFunction":
        return FakeEmbeddingFunction()
