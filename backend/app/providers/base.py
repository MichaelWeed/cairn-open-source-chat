from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.api.contracts import ProviderGenerationRequest
from app.providers.contracts import ProviderStreamEvent


class Provider(ABC):
    """Streams a text reply for one turn of conversation.

    Implementations yield text deltas and must let exceptions propagate on
    failure (connection errors, non-2xx responses, etc.) — mapping those to
    the `provider_unavailable` SSE error is the chat endpoint's job (task
    1.7), not the adapter's.

    The request is validated and server-owned. Public request context is not
    passed through this seam. Implementations may ignore retrieved context
    when grounding is intentionally unsupported (for example EchoProvider).
    """

    @abstractmethod
    def stream(self, request: ProviderGenerationRequest) -> AsyncIterator[ProviderStreamEvent]: ...
