from app.api.contracts import ProviderGenerationRequest
from app.providers.echo import EchoProvider


def request(message: str = "hello there world", **changes: object) -> ProviderGenerationRequest:
    data: dict[str, object] = {
        "system_instruction": "",
        "message": message,
        "history": [],
        "retrieved_context": "",
        "max_output_tokens": 1500,
        "max_output_chars": 6000,
    }
    data.update(changes)
    return ProviderGenerationRequest.model_validate(data)


async def test_echo_reconstructs_message() -> None:
    provider = EchoProvider()
    chunks = [chunk async for chunk in provider.stream(request())]
    assert "".join(chunk.delta for chunk in chunks) == "hello there world"


async def test_echo_accepts_validated_generation_request() -> None:
    provider = EchoProvider()
    chunks = [chunk async for chunk in provider.stream(request("same input"))]
    assert "".join(chunk.delta for chunk in chunks) == "same input"


async def test_echo_is_deterministic() -> None:
    provider = EchoProvider()
    first = [chunk async for chunk in provider.stream(request("same input"))]
    second = [chunk async for chunk in provider.stream(request("same input"))]
    assert first == second


async def test_echo_ignores_context() -> None:
    provider = EchoProvider()
    chunks = [
        chunk
        async for chunk in provider.stream(
            request("hello world", retrieved_context="irrelevant")
        )
    ]
    assert "".join(chunk.delta for chunk in chunks) == "hello world"
