from app.api.contracts import ProviderGenerationRequest
from app.providers.contracts import ProviderTextChunk
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


async def _collect_text(request_value: ProviderGenerationRequest) -> list[ProviderTextChunk]:
    events = [event async for event in EchoProvider().stream(request_value)]
    assert all(isinstance(event, ProviderTextChunk) for event in events)
    return [event for event in events if isinstance(event, ProviderTextChunk)]


async def test_echo_reconstructs_message() -> None:
    chunks = await _collect_text(request())
    assert "".join(chunk.delta for chunk in chunks) == "hello there world"


async def test_echo_accepts_validated_generation_request() -> None:
    chunks = await _collect_text(request("same input"))
    assert "".join(chunk.delta for chunk in chunks) == "same input"


async def test_echo_is_deterministic() -> None:
    first = await _collect_text(request("same input"))
    second = await _collect_text(request("same input"))
    assert first == second


async def test_echo_ignores_context() -> None:
    chunks = await _collect_text(
        request("hello world", retrieved_context="irrelevant")
    )
    assert "".join(chunk.delta for chunk in chunks) == "hello world"
