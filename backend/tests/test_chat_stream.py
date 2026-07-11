import asyncio
from collections.abc import AsyncIterator

from app.api.chat import stream_with_pings


async def _slow_source() -> AsyncIterator[str]:
    yield "fast"
    await asyncio.sleep(0.05)
    yield "after a pause"


async def test_ping_interleaved_on_quiet_source() -> None:
    events = [event async for event in stream_with_pings(_slow_source(), ping_interval=0.01)]
    types = [event.type for event in events]
    assert types[0] == "chunk"
    assert "ping" in types
    assert types[-1] == "chunk"


async def _fast_source() -> AsyncIterator[str]:
    yield "a"
    yield "b"


async def test_no_pings_when_source_is_fast() -> None:
    events = [event async for event in stream_with_pings(_fast_source(), ping_interval=1.0)]
    assert [event.type for event in events] == ["chunk", "chunk"]
