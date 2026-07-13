import json
from collections.abc import AsyncIterator

import httpx

from app.api.contracts import ChatTurn
from app.providers.base import Provider


class OllamaProvider(Provider):
    """Local-model provider (default), talking to Ollama's /api/chat.

    Connection errors and non-2xx responses propagate to the caller — see
    Provider.stream's docstring.
    """

    def __init__(self, base_url: str, model: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient()

    async def stream(
        self, *, message: str, history: list[ChatTurn], context: str | None = None
    ) -> AsyncIterator[str]:
        messages: list[dict[str, str]] = []
        if context is not None:
            messages.append({"role": "system", "content": context})
        messages.extend({"role": turn.role, "content": turn.content} for turn in history)
        messages.append({"role": "user", "content": message})

        async with self._client.stream(
            "POST",
            f"{self._base_url}/api/chat",
            json={"model": self._model, "messages": messages, "stream": True},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                content = data.get("message", {}).get("content")
                if content:
                    yield content
                if data.get("done"):
                    return
