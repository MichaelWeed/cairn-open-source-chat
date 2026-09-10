import json
from collections.abc import AsyncIterator

import httpx

from app.api.contracts import CHUNK_MAX_CHARS, ProviderChunk, ProviderGenerationRequest
from app.providers.base import Provider


class _InvalidProviderOutput(ValueError):
    """Content-free failure for output that cannot satisfy the provider seam."""


def _provider_chunk(delta: str) -> ProviderChunk:
    try:
        return ProviderChunk(delta=delta)
    except ValueError:
        raise _InvalidProviderOutput("Ollama returned invalid response content") from None


def _split_complete_chunks(content: str) -> tuple[list[ProviderChunk], str]:
    chunks: list[ProviderChunk] = []
    while len(content) > CHUNK_MAX_CHARS:
        split_at = CHUNK_MAX_CHARS
        if not content[CHUNK_MAX_CHARS:].strip():
            # Keep one non-whitespace character with a trailing whitespace run so
            # the remainder remains a valid, lossless provider chunk.
            split_at = len(content[:CHUNK_MAX_CHARS].rstrip()) - 1
        delta = content[:split_at]
        if not delta.strip():
            break
        chunks.append(_provider_chunk(delta))
        content = content[split_at:]
    return chunks, content


class OllamaProvider(Provider):
    """Local-model provider (default), talking to Ollama's /api/chat.

    Connection errors and non-2xx responses propagate to the caller — see
    Provider.stream's docstring.
    """

    # httpx's unconfigured default is a flat 5s across connect/read/write —
    # nowhere near enough for a local model's first token, let alone a full
    # reply. A real 8B model on ordinary hardware routinely takes 10-20+
    # seconds; a "thinking"/reasoning-style model longer still, since it
    # generates hidden reasoning tokens before anything user-visible.
    # Confirmed live: this was a 100%-reproducible false "provider
    # unavailable" on every single request, not an edge case. Keep the
    # connect timeout tight (fail fast if Ollama itself isn't reachable)
    # but give read/write room for genuine local-model latency.
    _DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)

    def __init__(self, base_url: str, model: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=self._DEFAULT_TIMEOUT)

    async def stream(self, request: ProviderGenerationRequest) -> AsyncIterator[ProviderChunk]:
        messages: list[dict[str, str]] = []
        if request.system_instruction:
            messages.append({"role": "system", "content": request.system_instruction})
        if request.retrieved_context:
            messages.append({"role": "system", "content": request.retrieved_context})
        messages.extend({"role": turn.role, "content": turn.content} for turn in request.history)
        messages.append({"role": "user", "content": request.message})

        async with self._client.stream(
            "POST",
            f"{self._base_url}/api/chat",
            json={
                "model": self._model,
                "messages": messages,
                "stream": True,
                "options": {"num_predict": request.max_output_tokens},
            },
        ) as response:
            response.raise_for_status()
            pending_content = ""
            async for line in response.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                content = data.get("message", {}).get("content")
                if content is not None and not isinstance(content, str):
                    raise _InvalidProviderOutput("Ollama returned invalid response content")
                if content:
                    if (
                        content.strip()
                        and pending_content.strip()
                        and len(pending_content) <= CHUNK_MAX_CHARS
                    ):
                        ready, pending_content = _split_complete_chunks(pending_content)
                        for chunk in ready:
                            yield chunk
                        if pending_content.strip():
                            yield _provider_chunk(pending_content)
                            pending_content = ""
                    pending_content += content
                    ready, pending_content = _split_complete_chunks(pending_content)
                    for chunk in ready:
                        yield chunk
                if data.get("done"):
                    break

            if pending_content:
                ready, pending_content = _split_complete_chunks(pending_content)
                for chunk in ready:
                    yield chunk
                if not pending_content.strip():
                    raise _InvalidProviderOutput(
                        "Ollama response whitespace cannot satisfy chunk bounds"
                    )
                yield _provider_chunk(pending_content)
