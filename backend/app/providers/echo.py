import asyncio
from collections.abc import AsyncIterator

from app.api.contracts import ProviderChunk, ProviderGenerationRequest
from app.providers.base import Provider


class EchoProvider(Provider):
    """Deterministic provider for tests and the demo page (task 1.8):

    replies with the input message, word by word. No network calls, no
    randomness — the point is a stable, offline-testable SSE round trip.
    Ignores `context` (retrieved-document grounding) for the same reason.
    """

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self._delay_seconds = delay_seconds

    async def stream(self, request: ProviderGenerationRequest) -> AsyncIterator[ProviderChunk]:
        words = request.message.split()
        for i, word in enumerate(words):
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            yield ProviderChunk(delta=word if i == len(words) - 1 else f"{word} ")
