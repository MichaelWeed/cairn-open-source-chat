import json

import httpx

from app.embeddings.ollama import OllamaEmbeddingFunction


def test_sends_batch_request_and_parses_embeddings() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        body = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
        return httpx.Response(200, content=json.dumps(body).encode(), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fn = OllamaEmbeddingFunction(
        base_url="http://ollama:11434/", model="nomic-embed-text", client=client
    )

    result = [list(v) for v in fn(["doc one", "doc two"])]

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["json"] == {"model": "nomic-embed-text", "input": ["doc one", "doc two"]}


def test_raises_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"{}", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fn = OllamaEmbeddingFunction(
        base_url="http://ollama:11434", model="nomic-embed-text", client=client
    )

    try:
        fn(["hi"])
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("expected HTTPStatusError")


def test_name_and_config_round_trip() -> None:
    fn = OllamaEmbeddingFunction(base_url="http://ollama:11434", model="nomic-embed-text")
    assert fn.name() == "ollama"
    rebuilt = OllamaEmbeddingFunction.build_from_config(fn.get_config())
    assert rebuilt.name() == "ollama"
