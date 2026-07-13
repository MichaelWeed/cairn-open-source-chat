from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.api.contracts import ChatTurn


class Provider(ABC):
    """Streams a text reply for one turn of conversation.

    Implementations yield text deltas and must let exceptions propagate on
    failure (connection errors, non-2xx responses, etc.) — mapping those to
    the `provider_unavailable` SSE error is the chat endpoint's job (task
    1.7), not the adapter's.

    `context`, when given, is the retrieval pipeline's fully-formatted,
    delimited, untrusted-context block (task 2.4, see app/retrieval.py) —
    already assembled, adapters just need to place it ahead of the user
    turn. Adapters that don't ground on retrieved context (e.g.
    EchoProvider) are free to ignore it.
    """

    @abstractmethod
    def stream(
        self, *, message: str, history: list[ChatTurn], context: str | None = None
    ) -> AsyncIterator[str]: ...
