"""Local embedding types shared by the vector-store and providers."""

from collections.abc import Sequence
from typing import Any, Protocol

type EmbeddingInput = Sequence[str]
type EmbeddingVector = list[float]
type EmbeddingVectors = list[EmbeddingVector]
type Metadata = dict[str, Any]


class EmbeddingFunction(Protocol):
    """Synchronous local embedding surface used by the embedded vector store."""

    def __call__(self, input: EmbeddingInput) -> EmbeddingVectors: ...
