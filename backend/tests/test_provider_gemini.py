import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from app.api.contracts import ChatTurn, ProviderGenerationRequest
from app.providers.contracts import (
    ProviderStreamEvent,
    ProviderTextChunk,
    ProviderUsageChunk,
)
from app.providers.gemini import (
    GeminiProvider,
    GeminiProviderError,
    create_gemini_provider,
)
from app.request_accounting import (
    ProviderAttemptPolicy,
    RequestAccountingError,
    RequestAccountingSession,
)


class _HttpOptions:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_version: str | None = None,
        timeout: int,
        retry_options: object | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_version = api_version
        self.timeout = timeout
        self.retry_options = retry_options


class _ThinkingConfig:
    def __init__(self, *, thinking_level: str) -> None:
        self.thinking_level = thinking_level


class _GenerateContentConfig:
    def __init__(self, **values: object) -> None:
        vars(self).update(values)


class _Part:
    @classmethod
    def from_text(cls, *, text: str) -> object:
        return SimpleNamespace(text=text)


class _Content:
    def __init__(self, *, role: str, parts: list[object]) -> None:
        self.role = role
        self.parts = parts


class _Types:
    HttpOptions = _HttpOptions
    ThinkingConfig = _ThinkingConfig
    GenerateContentConfig = _GenerateContentConfig
    Part = _Part
    Content = _Content


types = _Types()


@dataclass
class _Item:
    text: object = None
    prompt_feedback: object = None
    candidates: object = None
    usage_metadata: object = None


class _Stream(AsyncIterator[object]):
    def __init__(self, items: Iterable[object]) -> None:
        self.items = iter(items)
        self.closed = False

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> object:
        try:
            item = next(self.items)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class _ThrowingCloseStream(_Stream):
    def __init__(self, items: Iterable[object]) -> None:
        super().__init__(items)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.closed = True
        self.close_calls += 1
        raise RuntimeError("close-secret-sentinel")


class _Models:
    def __init__(self, plans: list[object], model_result: object | None = None) -> None:
        self.plans = plans
        self.model_result = model_result
        self.calls: list[dict[str, object]] = []
        self.get_calls: list[str] = []

    async def generate_content_stream(self, **kwargs: object) -> AsyncIterator[object]:
        self.calls.append(kwargs)
        plan = self.plans.pop(0)
        if isinstance(plan, BaseException):
            raise plan
        return plan  # type: ignore[return-value]

    async def get(self, *, model: str) -> object:
        self.get_calls.append(model)
        if isinstance(self.model_result, BaseException):
            raise self.model_result
        return self.model_result


class _Client:
    def __init__(self, models: _Models) -> None:
        self.models = models
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _StatusError(Exception):
    def __init__(self, code: int, detail: str = "raw-provider-detail") -> None:
        super().__init__(detail)
        self.code = code


def _request(**changes: object) -> ProviderGenerationRequest:
    values: dict[str, object] = {
        "system_instruction": "System policy",
        "message": "Current question",
        "history": [
            ChatTurn(role="user", content="Earlier question"),
            ChatTurn(role="assistant", content="Earlier answer"),
        ],
        "retrieved_context": "Untrusted support context",
        "max_output_tokens": 321,
        "max_output_chars": 6000,
    }
    values.update(changes)
    return ProviderGenerationRequest.model_validate(values)


def _provider(models: _Models, **changes: object) -> GeminiProvider:
    values: dict[str, object] = {
        "client": _Client(models),
        "types_module": types,
        "model": "gemini-3.8-flash",
        "timeout_seconds": 0.2,
        "max_retries": 1,
    }
    values.update(changes)
    return GeminiProvider(**values)  # type: ignore[arg-type]


async def _collect(provider: GeminiProvider) -> list[str]:
    events = [event async for event in provider.stream(_request())]
    return [event.delta for event in events if isinstance(event, ProviderTextChunk)]


async def _collect_events(provider: GeminiProvider) -> list[ProviderStreamEvent]:
    return [event async for event in provider.stream(_request())]


async def test_request_shape_uses_frozen_sdk_configuration_and_context_as_data() -> None:
    models = _Models([_Stream([_Item(text="answer")])])
    provider = _provider(models, timeout_seconds=30.0)

    assert await _collect(provider) == ["answer"]

    call = models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    config: Any = call["config"]
    assert config.system_instruction == "System policy"
    assert config.temperature == 0.2
    assert config.max_output_tokens == 321
    assert str(config.thinking_config.thinking_level).lower().endswith("low")
    assert config.http_options.timeout == 30_000
    assert config.http_options.retry_options is None
    contents: list[Any] = call["contents"]  # type: ignore[assignment]
    assert [content.role for content in contents] == ["user", "model", "user"]
    assert contents[0].parts[0].text == "Earlier question"
    assert contents[1].parts[0].text == "Earlier answer"
    final = json.loads(contents[2].parts[0].text)
    assert final == {
        "retrieved_support_context": "Untrusted support context",
        "visitor_question": "Current question",
    }


@pytest.mark.parametrize(
    "items,expected",
    [
        ([" leading", " \n", "middle", " trailing "], " leading \nmiddle trailing "),
        (["x" * 1000, " "], "x" * 1000 + " "),
        (["😀" * 1001], "😀" * 1001),
        ([" \n", "answer"], " \nanswer"),
    ],
)
async def test_streaming_is_lossless_and_emits_only_provider_chunks(
    items: list[str], expected: str
) -> None:
    provider = _provider(_Models([_Stream([_Item(text=item) for item in items])]))

    chunks = await _collect(provider)

    assert "".join(chunks) == expected
    assert all(chunk.strip() for chunk in chunks)
    assert all(1 <= len(chunk) <= 1000 for chunk in chunks)


@pytest.mark.parametrize("items", [[_Item(text=" \n")], [_Item(text=""), _Item()]])
async def test_textless_or_all_whitespace_completion_is_nonretryable(items: list[_Item]) -> None:
    models = _Models([_Stream(items)])
    provider = _provider(models)

    with pytest.raises(GeminiProviderError) as caught:
        await _collect(provider)

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert caught.value.attempt_count == 1
    assert len(models.calls) == 1


async def test_usage_only_item_is_emitted_then_textless_failure_is_not_retried() -> None:
    models = _Models([_Stream([_Item(usage_metadata=SimpleNamespace(total_token_count=12))])])
    provider = _provider(models)
    iterator = provider.stream(_request())

    usage = await anext(iterator)
    assert isinstance(usage, ProviderUsageChunk)
    assert usage.usage.total_tokens == 12

    with pytest.raises(GeminiProviderError) as caught:
        await anext(iterator)

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert len(models.calls) == 1


async def test_usage_after_pre_item_retry_identifies_second_attempt() -> None:
    async def no_delay(_: float) -> None:
        return None

    models = _Models(
        [
            _StatusError(503, "retryable-sentinel"),
            _Stream(
                [
                    _Item(
                        text="answer",
                        usage_metadata=SimpleNamespace(prompt_token_count=4),
                    )
                ]
            ),
        ]
    )

    events = await _collect_events(_provider(models, sleep=no_delay))

    usage = next(event for event in events if isinstance(event, ProviderUsageChunk))
    assert usage.provider_attempt == 2
    assert len(models.calls) == 2


async def test_usage_is_self_identifying_cumulative_and_precedes_same_item_text() -> None:
    first = SimpleNamespace(
        prompt_token_count=10,
        cached_content_token_count=2,
        candidates_token_count=3,
        thoughts_token_count=None,
        total_token_count=None,
        traffic_type=None,
    )
    final = SimpleNamespace(
        prompt_token_count=10,
        cached_content_token_count=2,
        candidates_token_count=4,
        thoughts_token_count=1,
        total_token_count=15,
        traffic_type="priority",
    )
    provider = _provider(
        _Models(
            [
                _Stream(
                    [
                        _Item(usage_metadata=first),
                        _Item(text="answer", usage_metadata=final),
                    ]
                )
            ]
        )
    )

    events = await _collect_events(provider)

    assert [event.kind for event in events] == ["usage", "usage", "text"]
    usage = events[1]
    assert isinstance(usage, ProviderUsageChunk)
    assert usage.provider == "gemini"
    assert usage.model == "gemini-3.8-flash"
    assert usage.provider_attempt == 1
    assert usage.service_tier == "priority"
    assert usage.usage.model_dump() == {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 4,
        "thinking_tokens": 1,
        "total_tokens": 15,
    }


@pytest.mark.parametrize(
    "metadata",
    [
        SimpleNamespace(prompt_token_count=True),
        SimpleNamespace(prompt_token_count=-1),
        SimpleNamespace(prompt_token_count=1, cached_content_token_count=2),
        SimpleNamespace(
            prompt_token_count=2,
            candidates_token_count=1,
            thoughts_token_count=0,
            total_token_count=4,
        ),
        SimpleNamespace(prompt_token_count=1, traffic_type=" padded "),
        SimpleNamespace(prompt_token_count=1, traffic_type=SimpleNamespace(name="PRIORITY")),
    ],
)
async def test_malformed_usage_is_content_free_nonretryable(metadata: object) -> None:
    models = _Models([_Stream([_Item(text="must-not-escape", usage_metadata=metadata)])])

    with pytest.raises(GeminiProviderError) as caught:
        await _collect_events(_provider(models))

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert caught.value.attempt_count == 1
    assert "must-not-escape" not in str(caught.value)
    assert len(models.calls) == 1


async def test_regressive_usage_is_rejected_before_same_item_text() -> None:
    models = _Models(
        [
            _Stream(
                [
                    _Item(
                        usage_metadata=SimpleNamespace(prompt_token_count=10),
                    ),
                    _Item(
                        text="must-not-escape",
                        usage_metadata=SimpleNamespace(prompt_token_count=9),
                    ),
                ]
            )
        ]
    )

    with pytest.raises(GeminiProviderError) as caught:
        await _collect_events(_provider(models))

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert "must-not-escape" not in str(caught.value)
    assert len(models.calls) == 1


async def test_transient_failure_retries_only_before_any_sdk_item() -> None:
    delay_calls: list[float] = []

    async def delay(seconds: float) -> None:
        delay_calls.append(seconds)

    models = _Models([_StatusError(429), _Stream([_Item(text="ok")])])
    provider = _provider(models, sleep=delay)

    assert await _collect(provider) == ["ok"]
    assert len(models.calls) == 2
    assert delay_calls == [0.1]

    observed_stream = _Stream([_Item(text="partial"), _StatusError(503)])
    observed_models = _Models([observed_stream, _Stream([_Item(text="wrong")])])
    observed_provider = _provider(observed_models, sleep=delay)
    with pytest.raises(GeminiProviderError) as caught:
        await _collect(observed_provider)
    assert caught.value.code == "provider_unavailable"
    assert caught.value.attempt_count == 1
    assert len(observed_models.calls) == 1
    assert observed_stream.closed is True


async def test_retry_attempts_have_distinct_accounting_histories_before_sleep() -> None:
    delay_observed_attempts: list[int] = []
    session = RequestAccountingSession(
        attempt_date=date(2026, 9, 10),
        price_snapshots=(),
        policy=ProviderAttemptPolicy(provider="gemini", model="gemini-3.8-flash", max_attempts=2),
    )

    async def delay(_: float) -> None:
        delay_observed_attempts.append(len(session._attempts))
        assert session._attempts[0].completion == "error"

    models = _Models([_StatusError(503), _Stream([_Item(text="ok")])])
    provider = _provider(models, sleep=delay)
    events = [event async for event in provider.stream(_request(), observer=session)]
    assert [event.kind for event in events] == ["text"]
    summary = session.finalize("completed")
    assert delay_observed_attempts == [1]
    assert [attempt.identity.provider_attempt for attempt in summary.attempts] == [1, 2]
    settled = [cast(Any, attempt).cost_record for attempt in summary.attempts]
    assert [record.completion_state for record in settled] == [
        "error",
        "completed",
    ]


async def test_gemini_observer_start_failure_prevents_sdk_and_retry() -> None:
    class FailedObserver:
        def attempt_started(self, identity: object) -> None:
            del identity
            raise RuntimeError("OBSERVER-CANARY")

    models = _Models([_Stream([_Item(text="must-not-run")])])
    with pytest.raises(RequestAccountingError) as caught:
        await anext(_provider(models).stream(_request(), observer=FailedObserver()))  # type: ignore[arg-type]
    assert models.calls == []
    assert "OBSERVER-CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (408, "provider_timeout", True),
        (429, "rate_limited", True),
        (400, "invalid_request", False),
        (503, "provider_unavailable", True),
    ],
)
async def test_http_errors_are_normalized_without_raw_details(
    status: int, code: str, retryable: bool
) -> None:
    provider = _provider(_Models([_StatusError(status)]), max_retries=0)

    with pytest.raises(GeminiProviderError) as caught:
        await _collect(provider)

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "raw-provider-detail" not in str(caught.value)
    assert set(vars(caught.value)) == {"code", "message", "retryable", "attempt_count"}


async def test_transport_failure_is_normalized_and_retryable() -> None:
    request = httpx.Request("POST", "https://provider.invalid")
    provider = _provider(
        _Models([httpx.ConnectError("raw-transport-detail", request=request)]),
        max_retries=0,
    )

    with pytest.raises(GeminiProviderError) as caught:
        await _collect(provider)

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is True
    assert "raw-transport-detail" not in str(caught.value)


async def test_safety_block_is_normalized_and_not_retried() -> None:
    item = _Item(
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
        candidates=[],
    )
    models = _Models([_Stream([item]), _Stream([_Item(text="wrong")])])
    provider = _provider(models)

    with pytest.raises(GeminiProviderError) as caught:
        await _collect(provider)

    assert caught.value.code == "guardrail_block"
    assert caught.value.retryable is False
    assert len(models.calls) == 1


async def test_safety_block_yields_returned_usage_before_content_free_failure() -> None:
    stream = _Stream(
        [
            _Item(
                text="must-not-escape",
                prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
                candidates=[],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=17,
                    candidates_token_count=3,
                    thoughts_token_count=1,
                    total_token_count=21,
                ),
            )
        ]
    )
    models = _Models([stream, _Stream([_Item(text="wrong-retry")])])
    iterator = _provider(models).stream(_request())

    usage = await anext(iterator)
    assert isinstance(usage, ProviderUsageChunk)
    assert usage.provider == "gemini"
    assert usage.model == "gemini-3.8-flash"
    assert usage.provider_attempt == 1
    assert usage.usage.input_tokens == 17
    assert usage.usage.output_tokens == 3
    assert usage.usage.thinking_tokens == 1
    assert usage.usage.total_tokens == 21

    with pytest.raises(GeminiProviderError) as caught:
        await anext(iterator)

    assert caught.value.code == "guardrail_block"
    assert caught.value.retryable is False
    assert caught.value.attempt_count == 1
    assert "must-not-escape" not in str(caught.value)
    assert len(models.calls) == 1
    assert stream.closed is True


async def test_malformed_safety_usage_precedes_guardrail_without_text_or_retry() -> None:
    stream = _Stream(
        [
            _Item(
                text="must-not-escape",
                prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
                candidates=[],
                usage_metadata=SimpleNamespace(prompt_token_count=True),
            )
        ]
    )
    models = _Models([stream, _Stream([_Item(text="wrong-retry")])])

    with pytest.raises(GeminiProviderError) as caught:
        await _collect_events(_provider(models))

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert caught.value.attempt_count == 1
    assert "must-not-escape" not in str(caught.value)
    assert len(models.calls) == 1
    assert stream.closed is True


async def test_regressive_safety_usage_fails_after_prior_usage_without_text_or_retry() -> None:
    stream = _Stream(
        [
            _Item(usage_metadata=SimpleNamespace(prompt_token_count=10)),
            _Item(
                text="must-not-escape",
                prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
                candidates=[],
                usage_metadata=SimpleNamespace(prompt_token_count=9),
            ),
        ]
    )
    models = _Models([stream, _Stream([_Item(text="wrong-retry")])])
    iterator = _provider(models).stream(_request())

    first = await anext(iterator)
    assert isinstance(first, ProviderUsageChunk)
    assert first.usage.input_tokens == 10
    with pytest.raises(GeminiProviderError) as caught:
        await anext(iterator)

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert caught.value.attempt_count == 1
    assert "must-not-escape" not in str(caught.value)
    assert len(models.calls) == 1
    assert stream.closed is True


async def test_stream_closes_on_normal_completion_and_cancellation() -> None:
    stream = _Stream([_Item(text="ok")])
    provider = _provider(_Models([stream]))
    assert await _collect(provider) == ["ok"]
    assert stream.closed is True

    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingStream(_Stream):
        async def __anext__(self) -> object:
            started.set()
            await release.wait()
            return _Item(text="late")

    blocking = _BlockingStream([])
    provider = _provider(_Models([blocking]))
    task = asyncio.create_task(_collect(provider))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert blocking.closed is True


async def test_throwing_close_is_normalized_after_successful_completion() -> None:
    stream = _ThrowingCloseStream([_Item(text="ok")])
    iterator = _provider(_Models([stream]), max_retries=0).stream(_request())

    event = await anext(iterator)
    assert isinstance(event, ProviderTextChunk)
    assert event.delta == "ok"
    with pytest.raises(GeminiProviderError) as caught:
        await anext(iterator)

    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert caught.value.attempt_count == 1
    assert "close-secret-sentinel" not in str(caught.value)
    assert stream.close_calls == 1


async def test_throwing_close_preserves_generation_failure_and_timeout() -> None:
    failed_stream = _ThrowingCloseStream([_StatusError(503)])
    provider = _provider(_Models([failed_stream]), max_retries=0)
    with pytest.raises(GeminiProviderError) as caught:
        await _collect(provider)
    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is True
    assert "close-secret-sentinel" not in str(caught.value)
    assert failed_stream.close_calls == 1

    release = asyncio.Event()

    class _BlockingCloseStream(_ThrowingCloseStream):
        async def __anext__(self) -> object:
            await release.wait()
            return _Item(text="late")

    timed_stream = _BlockingCloseStream([])
    provider = _provider(_Models([timed_stream]), timeout_seconds=0.001, max_retries=0)
    with pytest.raises(GeminiProviderError) as caught:
        await _collect(provider)
    assert caught.value.code == "provider_timeout"
    assert caught.value.retryable is True
    assert "close-secret-sentinel" not in str(caught.value)
    assert timed_stream.close_calls == 1


async def test_throwing_close_does_not_replace_early_close_or_cancellation() -> None:
    early_stream = _ThrowingCloseStream([_Item(text="first"), _Item(text="second")])
    iterator = _provider(_Models([early_stream]), max_retries=0).stream(_request())
    event = await anext(iterator)
    assert isinstance(event, ProviderTextChunk)
    assert event.delta == "first"
    await cast(Any, iterator).aclose()
    assert early_stream.close_calls == 1

    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingCloseStream(_ThrowingCloseStream):
        async def __anext__(self) -> object:
            started.set()
            await release.wait()
            return _Item(text="late")

    cancelled_stream = _BlockingCloseStream([])
    task = asyncio.create_task(_collect(_provider(_Models([cancelled_stream]), timeout_seconds=1)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_stream.close_calls == 1


async def test_cancellation_during_cleanup_replaces_recorded_provider_failure() -> None:
    close_started = asyncio.Event()

    class _BlockingCloseStream(_Stream):
        def __init__(self) -> None:
            super().__init__([_StatusError(503)])
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            close_started.set()
            await asyncio.Event().wait()

    stream = _BlockingCloseStream()
    task = asyncio.create_task(_collect(_provider(_Models([stream]), max_retries=0)))
    await close_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.close_calls == 1


async def test_internally_raised_close_cancellation_obeys_failure_precedence() -> None:
    class _InternallyCancelledCloseStream(_Stream):
        def __init__(self, items: Iterable[object]) -> None:
            super().__init__(items)
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            raise asyncio.CancelledError

    failed_stream = _InternallyCancelledCloseStream([_StatusError(503)])
    with pytest.raises(GeminiProviderError) as caught:
        await _collect(_provider(_Models([failed_stream]), max_retries=0))
    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is True
    assert failed_stream.close_calls == 1

    completed_stream = _InternallyCancelledCloseStream([_Item(text="ok")])
    iterator = _provider(_Models([completed_stream]), max_retries=0).stream(_request())
    event = await anext(iterator)
    assert isinstance(event, ProviderTextChunk)
    assert event.delta == "ok"
    with pytest.raises(GeminiProviderError) as caught:
        await anext(iterator)
    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is False
    assert completed_stream.close_calls == 1


async def test_readiness_requires_exact_model_and_generation_method() -> None:
    ready_model = SimpleNamespace(
        name="models/gemini-3.8-flash",
        supported_generation_methods=["generateContent"],
    )
    provider = _provider(_Models([], model_result=ready_model))
    readiness = await provider.check_readiness()
    assert readiness.reachable is True
    assert readiness.model_ready is True

    for result in [
        SimpleNamespace(name="models/other", supported_generation_methods=["generateContent"]),
        SimpleNamespace(name="models/gemini-3.8-flash", supported_generation_methods=[]),
        RuntimeError("private readiness detail"),
    ]:
        not_ready = await _provider(_Models([], model_result=result)).check_readiness()
        assert not_ready.model_ready is False
        assert not_ready.reachable is (not isinstance(result, BaseException))


async def test_malformed_non_string_and_unrepresentable_whitespace_are_rejected() -> None:
    for item in [
        _Item(text=123),
        _Item(text=" " * 1000 + "x"),
        _Item(text="x" + " " * 1000),
    ]:
        provider = _provider(_Models([_Stream([item])]))
        with pytest.raises(GeminiProviderError) as caught:
            await _collect(provider)
        assert caught.value.code == "provider_unavailable"
        assert caught.value.retryable is False


async def test_candidate_safety_finish_reason_is_blocked() -> None:
    candidate = SimpleNamespace(finish_reason="SAFETY")
    provider = _provider(_Models([_Stream([_Item(candidates=[candidate])])]))
    with pytest.raises(GeminiProviderError) as caught:
        await _collect(provider)
    assert caught.value.code == "guardrail_block"


async def test_timeout_covers_stream_creation_and_iteration() -> None:
    release = asyncio.Event()

    class _CreationModels(_Models):
        async def generate_content_stream(self, **kwargs: object) -> AsyncIterator[object]:
            self.calls.append(kwargs)
            await release.wait()
            return _Stream([])

    creation = _CreationModels([])
    with pytest.raises(GeminiProviderError) as caught:
        await _collect(_provider(creation, timeout_seconds=0.001, max_retries=0))
    assert caught.value.code == "provider_timeout"
    assert caught.value.retryable is True

    class _IterationStream(_Stream):
        async def __anext__(self) -> object:
            await release.wait()
            return _Item(text="late")

    stream = _IterationStream([])
    with pytest.raises(GeminiProviderError) as caught:
        await _collect(_provider(_Models([stream]), timeout_seconds=0.001, max_retries=0))
    assert caught.value.code == "provider_timeout"
    assert stream.closed is True


async def test_cancellation_propagates_during_creation_and_retry_delay() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _CreationModels(_Models):
        async def generate_content_stream(self, **kwargs: object) -> AsyncIterator[object]:
            self.calls.append(kwargs)
            started.set()
            await release.wait()
            return _Stream([])

    creation_provider = _provider(_CreationModels([]), timeout_seconds=1)
    task = asyncio.create_task(_collect(creation_provider))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    delay_started = asyncio.Event()

    async def delay(_: float) -> None:
        delay_started.set()
        await release.wait()

    retry_provider = _provider(_Models([_StatusError(503)]), sleep=delay)
    task = asyncio.create_task(_collect(retry_provider))
    await delay_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_readiness_timeout_and_cancellation_are_content_free() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _ReadinessModels(_Models):
        async def get(self, *, model: str) -> object:
            self.get_calls.append(model)
            started.set()
            await release.wait()
            return SimpleNamespace(name=model, supported_generation_methods=["generateContent"])

    timed = await _provider(_ReadinessModels([]), timeout_seconds=0.001).check_readiness()
    assert timed.reachable is False
    assert timed.model_ready is False

    started.clear()
    models = _ReadinessModels([])
    task = asyncio.create_task(_provider(models, timeout_seconds=1).check_readiness())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_factory_uses_explicit_v1beta_client_options_and_closes_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _RootClient:
        def __init__(self, *, enterprise: bool, api_key: str, http_options: object) -> None:
            captured["enterprise"] = enterprise
            captured["api_key"] = api_key
            captured["http_options"] = http_options
            self.aio = _Client(_Models([]))
            self.close_calls = 0
            captured["root"] = self

        def close(self) -> None:
            self.close_calls += 1

    def dynamic_import(name: str) -> object:
        if name == "google.genai":
            return SimpleNamespace(Client=_RootClient)
        if name == "google.genai.types":
            return types
        raise AssertionError(name)

    monkeypatch.setattr("app.providers.gemini.importlib.import_module", dynamic_import)
    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "true")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://ambient.invalid/")
    provider = create_gemini_provider(
        api_key="sentinel-api-key",
        model="gemini-3.8-flash",
        timeout_seconds=30.0,
        max_retries=1,
    )
    options: Any = captured["http_options"]
    root: Any = captured["root"]
    assert captured["enterprise"] is False
    assert captured["api_key"] == "sentinel-api-key"
    assert options.base_url == "https://generativelanguage.googleapis.com/"
    assert options.api_version == "v1beta"
    assert options.timeout == 30_000
    assert options.retry_options is None
    await provider.aclose()
    await provider.aclose()
    assert root.aio.closed is True
    assert root.close_calls == 1
