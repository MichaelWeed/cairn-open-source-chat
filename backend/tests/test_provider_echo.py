from app.providers.echo import EchoProvider


async def test_echo_reconstructs_message() -> None:
    provider = EchoProvider()
    chunks = [chunk async for chunk in provider.stream(message="hello there world", history=[])]
    assert "".join(chunks) == "hello there world"


async def test_echo_empty_message_yields_nothing() -> None:
    provider = EchoProvider()
    chunks = [chunk async for chunk in provider.stream(message="", history=[])]
    assert chunks == []


async def test_echo_is_deterministic() -> None:
    provider = EchoProvider()
    first = [chunk async for chunk in provider.stream(message="same input", history=[])]
    second = [chunk async for chunk in provider.stream(message="same input", history=[])]
    assert first == second
