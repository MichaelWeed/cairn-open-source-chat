import asyncio
import gc
import warnings
import weakref
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from pydantic import TypeAdapter

from app.api.contracts import ProviderGenerationRequest
from app.config import Settings
from app.providers.accounting import (
    ProviderAttemptAccountingInput,
    ProviderAttemptCostRecord,
    ProviderPriceSnapshot,
    compute_price_snapshot_id,
    price_provider_attempt,
)
from app.providers.contracts import ProviderUsage, ProviderUsageChunk
from app.providers.echo import EchoProvider
from app.providers.gemini import GeminiProvider
from app.providers.ollama import OllamaProvider
from app.request_accounting import (
    AttemptIdentity,
    ControlledProviderAccountingBinding,
    ProviderAttemptPolicy,
    RequestAccountingError,
    RequestAccountingSession,
    RequestAccountingSessionFactory,
    RequestAccountingSummary,
    SettledAttempt,
    UncertainAttempt,
    bind_application_owned_provider,
    controlled_provider_observer,
    provider_attempt_policy,
)

ATTEMPT_DATE = date(2026, 9, 10)


def _identity(attempt: int = 1) -> AttemptIdentity:
    return AttemptIdentity(provider="ollama", model="llama3.2", provider_attempt=attempt)


def _usage(*, input_tokens: int, output_tokens: int | None = None) -> ProviderUsageChunk:
    total = None if output_tokens is None else input_tokens + output_tokens
    return ProviderUsageChunk(
        provider="ollama",
        model="llama3.2",
        provider_attempt=1,
        usage=ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
        ),
    )


def _complete_usage() -> ProviderUsageChunk:
    return ProviderUsageChunk(
        provider="ollama",
        model="llama3.2",
        provider_attempt=1,
        usage=ProviderUsage(
            input_tokens=11,
            cached_input_tokens=2,
            output_tokens=3,
            thinking_tokens=1,
            total_tokens=15,
        ),
    )


def _snapshot() -> ProviderPriceSnapshot:
    provider = "ollama"
    model = "llama3.2"
    service_tier = None
    currency = "USD"
    effective_from = ATTEMPT_DATE
    effective_through = None
    source_url = "https://example.com/pricing"
    uncached = Decimal("1")
    cached = Decimal("0.5")
    output = Decimal("2")
    thinking = Decimal("3")
    return ProviderPriceSnapshot(
        snapshot_id=compute_price_snapshot_id(
            provider=provider,
            model=model,
            service_tier=service_tier,
            currency=currency,
            effective_from=effective_from,
            effective_through=effective_through,
            source_url=source_url,
            uncached_input_rate_per_million=uncached,
            cached_input_rate_per_million=cached,
            output_rate_per_million=output,
            thinking_rate_per_million=thinking,
        ),
        provider=provider,
        model=model,
        service_tier=service_tier,
        currency=currency,
        effective_from=effective_from,
        effective_through=effective_through,
        source_url=source_url,
        uncached_input_rate_per_million=uncached,
        cached_input_rate_per_million=cached,
        output_rate_per_million=output,
        thinking_rate_per_million=thinking,
    )


def test_session_merges_cumulative_usage_and_finalizes_once() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(),
        currency=None,
        max_attempts=1,
    )
    identity = _identity()
    session.attempt_started(identity)
    session.usage_observed(_usage(input_tokens=5))
    session.usage_observed(_usage(input_tokens=5, output_tokens=3))
    session.attempt_finished(identity, "completed")

    summary = session.finalize("completed")
    assert summary.provider_work_started is True
    assert summary.request_completion == "completed"
    assert len(summary.attempts) == 1
    attempt = summary.attempts[0]
    assert attempt.kind == "settled"
    assert attempt.cost_record.cost_state == "usage_incomplete"
    assert attempt.cost_record.usage == ProviderUsage(
        input_tokens=5,
        output_tokens=3,
        total_tokens=8,
    )
    assert session.finalize("completed") is summary
    with pytest.raises(RequestAccountingError):
        session.finalize("error")


def test_unfinished_attempt_is_never_priced() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(),
        currency=None,
        max_attempts=1,
    )
    session.attempt_started(_identity())
    session.usage_observed(_usage(input_tokens=5, output_tokens=3))

    summary = session.finalize("abandoned")
    attempt = summary.attempts[0]
    assert attempt.kind == "uncertain"
    assert attempt.reason == "finish_missing"


def test_factory_uses_date_source_once_and_explicit_date_bypasses_it() -> None:
    calls = 0

    def today() -> date:
        nonlocal calls
        calls += 1
        return ATTEMPT_DATE

    factory = RequestAccountingSessionFactory(
        policy=ProviderAttemptPolicy(provider="ollama", model="llama3.2", max_attempts=1),
        price_snapshots=(),
        currency=None,
        date_source=today,
    )
    assert factory.create().attempt_date == ATTEMPT_DATE
    assert calls == 1
    assert factory.create(attempt_date=date(2026, 9, 11)).attempt_date == date(2026, 9, 11)
    assert calls == 1


def test_errors_are_content_free_and_work_after_finalize_fails() -> None:
    secret = "REQUEST-SECRET-CANARY"
    with pytest.raises(RequestAccountingError) as caught:
        AttemptIdentity(provider=secret, model="m", provider_attempt=1)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(),
        currency=None,
        max_attempts=1,
    )
    session.finalize("refused")
    with pytest.raises(RequestAccountingError):
        session.attempt_started(_identity())


@pytest.mark.parametrize(
    "completion",
    ["completed", "refused", "limit", "error", "cancelled", "abandoned"],
)
def test_empty_summary_has_no_provider_work(completion: str) -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(),
        max_attempts=0,
    )
    summary = session.finalize(completion)  # type: ignore[arg-type]
    assert summary.provider_work_started is False
    assert summary.attempts == ()


def test_complete_usage_prices_exactly_once_with_m4_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.request_accounting as accounting

    calls = 0
    original = price_provider_attempt

    def counted(value: ProviderAttemptAccountingInput) -> ProviderAttemptCostRecord:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(accounting, "price_provider_attempt", counted, raising=False)
    snapshot = _snapshot()
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(snapshot,),
        currency="USD",
        max_attempts=1,
    )
    identity = _identity()
    usage = _complete_usage()
    session.attempt_started(identity)
    session.usage_observed(usage)
    session.attempt_finished(identity, "completed")
    record = cast(Any, session.finalize("completed").attempts[0]).cost_record

    expected = price_provider_attempt(
        ProviderAttemptAccountingInput(
            provider="ollama",
            model="llama3.2",
            provider_attempt=1,
            attempt_date=ATTEMPT_DATE,
            completion_state="completed",
            answer_outcome="grounded",
            usage=usage.usage,
            price_snapshot=snapshot,
        )
    )
    assert calls == 1
    assert record == expected
    assert type(record.model_cost) is Decimal


def test_duplicate_matching_snapshots_make_attempt_uncertain() -> None:
    snapshot = _snapshot()
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(snapshot, snapshot),
        currency="USD",
        max_attempts=1,
    )
    identity = _identity()
    session.attempt_started(identity)
    session.usage_observed(_complete_usage())
    session.attempt_finished(identity, "completed")
    attempt = session.finalize("completed").attempts[0]
    assert attempt.kind == "uncertain"
    assert attempt.reason == "observer_failure"


def test_snapshot_input_is_exact_tuple_and_bounded() -> None:
    snapshot = _snapshot()
    RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(snapshot,) * 16,
        max_attempts=1,
    )
    for rejected in ([snapshot], (snapshot,) * 17):
        with pytest.raises(RequestAccountingError):
            RequestAccountingSession(
                attempt_date=ATTEMPT_DATE,
                price_snapshots=rejected,  # type: ignore[arg-type]
                max_attempts=1,
            )


def test_state_machine_rejects_order_identity_and_regression_content_free() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(),
        max_attempts=2,
    )
    with pytest.raises(RequestAccountingError):
        session.usage_observed(_usage(input_tokens=1))
    with pytest.raises(RequestAccountingError):
        session.attempt_started(_identity(2))
    session.attempt_started(_identity())
    session.usage_observed(_usage(input_tokens=5))
    with pytest.raises(RequestAccountingError):
        session.usage_observed(_usage(input_tokens=4))
    attempt = session.finalize("error").attempts[0]
    assert attempt.kind == "uncertain"
    assert attempt.reason == "usage_invalid"


def test_attempt_two_requires_settled_error_attempt_one() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(),
        max_attempts=2,
    )
    session.attempt_started(_identity())
    session.attempt_finished(_identity(), "completed")
    with pytest.raises(RequestAccountingError):
        session.attempt_started(_identity(2))


def test_model_validation_and_copy_surfaces_are_sealed() -> None:
    valid = _identity()
    assert valid.model_copy() == valid
    rejected = {"provider": "BAD-CANARY", "model": "m", "provider_attempt": 1}
    operations: tuple[Callable[[], object], ...] = (
        lambda: AttemptIdentity.model_validate(rejected, extra="allow"),
        lambda: TypeAdapter(AttemptIdentity).validate_python(rejected),
        lambda: AttemptIdentity.model_construct(
            provider="BAD-CANARY", model="m", provider_attempt=1
        ),
        lambda: valid.model_copy(update={"provider": "BAD-CANARY"}),
        lambda: valid.copy(update={"provider": "BAD-CANARY"}),
        lambda: valid.__replace__(provider="BAD-CANARY"),
    )
    for operation in operations:
        with pytest.raises(RequestAccountingError) as caught:
            operation()
        assert "BAD-CANARY" not in str(caught.value)
        assert caught.value.__context__ is None

    with pytest.raises(RequestAccountingError):
        valid.provider = "ollama"


def test_mutated_m4_usage_is_rejected_before_retention() -> None:
    usage = _complete_usage()
    object.__getattribute__(usage.usage, "__dict__")["input_tokens"] = "RAW-CANARY"
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        price_snapshots=(),
        max_attempts=1,
    )
    session.attempt_started(_identity())
    with pytest.raises(RequestAccountingError) as caught:
        session.usage_observed(usage)
    assert "RAW-CANARY" not in str(caught.value)
    assert caught.value.__context__ is None
    assert session.finalize("error").attempts[0].kind == "uncertain"


def test_interleaved_sessions_do_not_exchange_attempts() -> None:
    first = RequestAccountingSession(attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1)
    second = RequestAccountingSession(attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1)
    first.attempt_started(_identity())
    second_summary = second.finalize("refused")
    first.attempt_finished(_identity(), "error")
    first_summary = first.finalize("error")
    assert second_summary.attempts == ()
    assert len(first_summary.attempts) == 1


@pytest.mark.parametrize(
    "values,expected",
    [
        ({"provider": "echo"}, ("echo", 0)),
        ({"provider": "ollama"}, ("ollama", 1)),
        (
            {"provider": "gemini", "gemini_api_key": "secret", "gemini_max_retries": 0},
            ("gemini", 1),
        ),
        (
            {"provider": "gemini", "gemini_api_key": "secret", "gemini_max_retries": 1},
            ("gemini", 2),
        ),
    ],
)
def test_settings_owned_attempt_policy(
    values: dict[str, object], expected: tuple[str, int]
) -> None:
    policy = provider_attempt_policy(Settings.model_validate(values))
    assert (policy.provider, policy.max_attempts) == expected


def test_factory_date_failures_are_content_free_and_datetime_is_rejected() -> None:
    def failed_date() -> date:
        raise RuntimeError("DATE-SOURCE-CANARY")

    policy = ProviderAttemptPolicy(provider="echo", model="", max_attempts=0)
    factory = RequestAccountingSessionFactory(policy=policy, date_source=failed_date)
    with pytest.raises(RequestAccountingError) as caught:
        factory.create()
    assert "DATE-SOURCE-CANARY" not in str(caught.value)
    assert caught.value.__context__ is None
    with pytest.raises(RequestAccountingError):
        factory.create(attempt_date=datetime(2026, 9, 10))


def test_hostile_mapping_hooks_are_not_invoked_by_model_validation() -> None:
    calls: list[str] = []

    class HostileDict(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            calls.append(key)
            raise RuntimeError("MAPPING-HOOK-CANARY")

        def items(self) -> Any:
            calls.append("items")
            raise RuntimeError("MAPPING-HOOK-CANARY")

    value = HostileDict(provider="ollama", model="m", provider_attempt=1)
    operations: tuple[Callable[[], object], ...] = (
        lambda: AttemptIdentity.model_validate(value),
        lambda: TypeAdapter(AttemptIdentity).validate_python(value),
    )
    for operation in operations:
        with pytest.raises(RequestAccountingError):
            operation()
    assert calls == []


def test_from_attributes_cannot_invoke_facsimile_hooks() -> None:
    calls: list[str] = []

    class HostileFacsimile:
        def __getattribute__(self, name: str) -> object:
            if not name.startswith("__"):
                calls.append(name)
                raise RuntimeError("ATTRIBUTE-HOOK-CANARY")
            return object.__getattribute__(self, name)

    value = HostileFacsimile()
    operations: tuple[Callable[[], object], ...] = (
        lambda: AttemptIdentity.model_validate(value, from_attributes=True),
        lambda: TypeAdapter(AttemptIdentity).validate_python(value, from_attributes=True),
    )
    for operation in operations:
        with pytest.raises(RequestAccountingError):
            operation()
    assert calls == []


def test_hostile_usage_discriminator_is_type_fenced_before_comparison() -> None:
    calls = 0

    class HostileStr(str):
        def __ne__(self, other: object) -> bool:
            nonlocal calls
            calls += 1
            raise RuntimeError("STRING-HOOK-CANARY")

    usage = _complete_usage()
    object.__getattribute__(usage, "__dict__")["kind"] = HostileStr("usage")
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1
    )
    session.attempt_started(_identity())
    with pytest.raises(RequestAccountingError):
        session.usage_observed(usage)
    assert calls == 0


def test_caller_boolean_cannot_forge_application_provider_authority() -> None:
    settings = Settings.model_validate({"provider": "echo"})
    with pytest.raises(RequestAccountingError):
        bind_application_owned_provider(
            settings,
            EchoProvider(),
            authority=True,
        )


def test_retry_identity_is_pinned_to_first_attempt() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=2
    )
    first = AttemptIdentity(provider="ollama", model="m1", provider_attempt=1)
    session.attempt_started(first)
    session.attempt_finished(first, "error")
    with pytest.raises(RequestAccountingError):
        session.attempt_started(AttemptIdentity(provider="gemini", model="m2", provider_attempt=2))


def test_valid_summary_python_json_and_type_adapter_round_trips() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1
    )
    identity = _identity()
    session.attempt_started(identity)
    session.attempt_finished(identity, "completed")
    summary = session.finalize("completed")
    summary_type = type(summary)
    assert summary_type.model_validate(summary.model_dump()) == summary
    assert (
        summary_type.model_validate(summary.model_dump(mode="python", round_trip=True)) == summary
    )
    assert summary_type.model_validate_json(summary.model_dump_json()) == summary
    adapter = TypeAdapter(summary_type)
    assert adapter.validate_python(summary.model_dump()) == summary
    for round_trip in (False, True):
        assert (
            adapter.validate_python(adapter.dump_python(summary, round_trip=round_trip)) == summary
        )

    dumped = summary.model_dump()
    dumped_attempt = cast(SettledAttempt, cast(tuple[object, ...], dumped["attempts"])[0])
    assert dumped_attempt is not summary.attempts[0]
    assert dumped_attempt.identity is not summary.attempts[0].identity
    object.__setattr__(dumped_attempt.identity, "model", "mutated-canary")
    assert summary.attempts[0].identity.model == "llama3.2"


def test_rejected_model_is_not_retained_by_our_validation_frames() -> None:
    identity = _identity()
    reference = weakref.ref(identity)
    object.__getattribute__(identity, "__dict__")["provider"] = object()
    caught: RequestAccountingError | None = None
    try:
        AttemptIdentity.model_validate(identity)
    except RequestAccountingError as error:
        caught = error
    del identity
    gc.collect()
    assert caught is not None
    assert reference() is None


def test_strict_false_cannot_reseal_corrupted_exact_model() -> None:
    identity = _identity()
    object.__getattribute__(identity, "__dict__")["provider_attempt"] = "1"
    operations: tuple[Callable[[], object], ...] = (
        lambda: AttemptIdentity.model_validate(identity, strict=False),
        lambda: TypeAdapter(AttemptIdentity).validate_python(identity, strict=False),
    )
    for operation in operations:
        with pytest.raises(RequestAccountingError):
            operation()


@pytest.mark.parametrize(
    "value,field,corrupt",
    [
        (_identity(), "provider_attempt", "1"),
        (
            RequestAccountingSummary(
                provider_work_started=False,
                request_completion="refused",
            ),
            "provider_work_started",
            0,
        ),
    ],
)
def test_dump_surfaces_reject_corrupted_exact_model(
    value: AttemptIdentity | RequestAccountingSummary,
    field: str,
    corrupt: object,
) -> None:
    object.__getattribute__(value, "__dict__")[field] = corrupt
    adapter = TypeAdapter(type(value))
    operations: tuple[Callable[[], object], ...] = (
        value.model_dump,
        lambda: value.model_dump(round_trip=True),
        value.model_dump_json,
        value.dict,
        value.json,
        lambda: adapter.dump_python(value),
        lambda: adapter.dump_python(value, round_trip=True),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for operation in operations:
            with pytest.raises(Exception) as caught:
                operation()
            assert "Request accounting input is invalid." in str(caught.value)


def test_hostile_scalar_subclasses_are_rejected_without_hooks() -> None:
    calls = 0

    class HostileStr(str):
        def __hash__(self) -> int:
            nonlocal calls
            calls += 1
            raise RuntimeError("SCALAR-HOOK-CANARY")

        def __eq__(self, other: object) -> bool:
            nonlocal calls
            calls += 1
            raise RuntimeError("SCALAR-HOOK-CANARY")

    for model, value in (
        (
            AttemptIdentity,
            {"provider": HostileStr("ollama"), "model": "m", "provider_attempt": 1},
        ),
        (
            ProviderAttemptPolicy,
            {"provider": HostileStr("echo"), "model": "", "max_attempts": 0},
        ),
    ):
        with pytest.raises(RequestAccountingError):
            model.model_validate(value)
    assert calls == 0


def test_rejected_usage_is_not_retained_by_fixed_error() -> None:
    usage = _complete_usage()
    reference = weakref.ref(usage)
    object.__getattribute__(usage, "__dict__")["kind"] = object()
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1
    )
    session.attempt_started(_identity())
    caught: RequestAccountingError | None = None
    try:
        session.usage_observed(usage)
    except RequestAccountingError as error:
        caught = error
    del usage
    gc.collect()
    assert caught is not None
    assert reference() is None


def test_exact_duplicate_usage_is_rejected_and_marks_usage_invalid() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1
    )
    identity = _identity()
    usage = _usage(input_tokens=5)
    session.attempt_started(identity)
    session.usage_observed(usage)
    with pytest.raises(RequestAccountingError):
        session.usage_observed(usage)
    attempt = session.finalize("error").attempts[0]
    assert attempt.kind == "uncertain"
    assert attempt.reason == "usage_invalid"


def test_rejected_snapshot_is_not_retained_by_fixed_error() -> None:
    snapshot = _snapshot()
    reference = weakref.ref(snapshot)
    object.__getattribute__(snapshot, "__dict__")["provider"] = object()
    caught: RequestAccountingError | None = None
    try:
        RequestAccountingSession(
            attempt_date=ATTEMPT_DATE,
            price_snapshots=(snapshot,),
            max_attempts=1,
        )
    except RequestAccountingError as error:
        caught = error
    del snapshot
    gc.collect()
    assert caught is not None
    assert reference() is None


def test_nested_m4_values_require_exact_python_objects_but_round_trip() -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1
    )
    identity = _identity()
    session.attempt_started(identity)
    session.attempt_finished(identity, "completed")
    settled = cast(SettledAttempt, session.finalize("completed").attempts[0])
    uncertain = UncertainAttempt(
        identity=identity,
        last_usage=ProviderUsage(input_tokens=1),
        reason="finish_missing",
    )

    with pytest.raises(RequestAccountingError):
        SettledAttempt(
            identity=identity,
            cost_record=settled.cost_record.model_dump(),  # type: ignore[arg-type]
        )
    with pytest.raises(RequestAccountingError):
        UncertainAttempt(
            identity=identity,
            last_usage=uncertain.last_usage.model_dump(),  # type: ignore[union-attr,arg-type]
            reason="finish_missing",
        )

    for value in (settled, uncertain):
        model_type = type(value)
        assert model_type.model_validate(value.model_dump()) == value
        assert model_type.model_validate_json(value.model_dump_json()) == value


def test_summary_rejects_hostile_nested_keys_without_invoking_hooks() -> None:
    calls = 0

    class HostileKey(str):
        def __hash__(self) -> int:
            return str.__hash__(self)

        def __eq__(self, other: object) -> bool:
            nonlocal calls
            calls += 1
            raise RuntimeError("NESTED-KEY-CANARY")

    hostile_attempt = {
        HostileKey("kind"): "uncertain",
        HostileKey("identity"): _identity(),
        HostileKey("service_tier"): None,
        HostileKey("completion"): "uncertain",
        HostileKey("last_usage"): None,
        HostileKey("reason"): "finish_missing",
    }
    calls = 0
    with pytest.raises(RequestAccountingError):
        RequestAccountingSummary.model_validate(
            {
                "provider_work_started": True,
                "request_completion": "error",
                "attempts": (hostile_attempt,),
            }
        )
    assert calls == 0


def test_settings_policy_rejects_hidden_state_and_hostile_scalar_without_hooks() -> None:
    calls = 0

    class HostileStr(str):
        def __eq__(self, other: object) -> bool:
            nonlocal calls
            calls += 1
            raise RuntimeError("SETTINGS-HOOK-CANARY")

    hostile = Settings(provider="echo")
    object.__getattribute__(hostile, "__dict__")["provider"] = HostileStr("echo")
    with pytest.raises(RequestAccountingError):
        provider_attempt_policy(hostile)
    assert calls == 0

    hidden = Settings(provider="echo")
    object.__setattr__(hidden, "__pydantic_extra__", {"private": "canary"})
    with pytest.raises(RequestAccountingError):
        provider_attempt_policy(hidden)


def test_factory_is_sealed_and_closes_unexpected_coroutine_result(
    recwarn: pytest.WarningsRecorder,
) -> None:
    policy = ProviderAttemptPolicy(provider="echo", model="", max_attempts=0)

    async def async_date() -> date:
        return ATTEMPT_DATE

    with pytest.raises(RequestAccountingError):
        RequestAccountingSessionFactory(
            policy=policy,
            date_source=cast(Callable[[], date], async_date),
        )

    coroutine = async_date()

    def returns_coroutine() -> date:
        return cast(date, coroutine)

    factory = RequestAccountingSessionFactory(policy=policy, date_source=returns_coroutine)
    with pytest.raises(RequestAccountingError):
        factory.create()
    assert cast(Any, coroutine).cr_frame is None
    assert not recwarn.list
    with pytest.raises(RequestAccountingError):
        factory._policy = policy


def test_factory_does_not_retain_invalid_date_result() -> None:
    class Token:
        pass

    token = Token()
    reference = weakref.ref(token)
    values = [token]

    def invalid_date() -> date:
        return cast(date, values.pop())

    factory = RequestAccountingSessionFactory(
        policy=ProviderAttemptPolicy(provider="echo", model="", max_attempts=0),
        date_source=invalid_date,
    )
    caught: RequestAccountingError | None = None
    try:
        factory.create()
    except RequestAccountingError as error:
        caught = error
    del token
    gc.collect()
    assert caught is not None
    assert reference() is None


def test_finalize_preserves_active_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.request_accounting as accounting

    cancellation = asyncio.CancelledError("FINALIZE-CANCEL-CANARY")

    def cancel_price(value: ProviderAttemptAccountingInput) -> ProviderAttemptCostRecord:
        del value
        raise cancellation

    monkeypatch.setattr(accounting, "price_provider_attempt", cancel_price)
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1
    )
    identity = _identity()
    session.attempt_started(identity)
    session.attempt_finished(identity, "completed")
    with pytest.raises(asyncio.CancelledError) as caught:
        session.finalize("completed")
    assert caught.value is cancellation


@pytest.mark.parametrize("operation", ["finished_identity", "finished_value", "uncertain"])
def test_malformed_finish_records_observer_failure(operation: str) -> None:
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE, price_snapshots=(), max_attempts=1
    )
    identity = _identity()
    session.attempt_started(identity)
    with pytest.raises(RequestAccountingError):
        if operation == "finished_identity":
            session.attempt_finished(cast(Any, object()), "completed")
        elif operation == "finished_value":
            session.attempt_finished(identity, cast(Any, True))
        else:
            session.attempt_uncertain(identity, cast(Any, True))
    attempt = session.finalize("error").attempts[0]
    assert attempt.kind == "uncertain"
    assert attempt.reason == "observer_failure"


def test_controlled_binding_constructor_is_not_public_authority() -> None:
    with pytest.raises(RequestAccountingError):
        ControlledProviderAccountingBinding(
            EchoProvider(),
            ProviderAttemptPolicy(provider="echo", model="", max_attempts=0),
        )


async def test_controlled_observer_rejects_provider_client_swap_before_io() -> None:
    import app.request_accounting as accounting

    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(500)

    original_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    replacement_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(provider="ollama", ollama_model="llama3.2")
    provider = OllamaProvider(
        settings.ollama_base_url,
        settings.ollama_model,
        client=original_client,
    )
    binding = bind_application_owned_provider(
        settings,
        provider,
        authority=accounting._APPLICATION_BINDING_AUTHORITY,
    )
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        policy=ProviderAttemptPolicy(provider="ollama", model="llama3.2", max_attempts=1),
    )
    observer = controlled_provider_observer(settings, provider, binding, session)
    provider._client = replacement_client
    request = ProviderGenerationRequest(
        system_instruction="",
        message="question",
        history=[],
        retrieved_context="context",
        max_output_tokens=10,
        max_output_chars=100,
    )
    try:
        with pytest.raises(RequestAccountingError):
            async for _ in provider.stream(request, observer=observer):
                pass
    finally:
        await original_client.aclose()
        await replacement_client.aclose()
    assert calls == 0
    assert session.finalize("error").attempts == ()


async def test_controlled_observer_rejects_gemini_types_swap_before_hooks() -> None:
    import app.request_accounting as accounting

    class HostileTypes:
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(f"types hook called: {name}")

    settings = Settings.model_validate(
        {
            "provider": "gemini",
            "gemini_api_key": "secret",
            "gemini_max_retries": 0,
        }
    )
    original_types = object()
    provider = GeminiProvider(
        client=cast(Any, object()),
        types_module=original_types,
        model=settings.gemini_model,
        timeout_seconds=settings.gemini_timeout_seconds,
        max_retries=settings.gemini_max_retries,
    )
    binding = bind_application_owned_provider(
        settings,
        provider,
        authority=accounting._APPLICATION_BINDING_AUTHORITY,
    )
    session = RequestAccountingSession(
        attempt_date=ATTEMPT_DATE,
        policy=ProviderAttemptPolicy(
            provider="gemini",
            model=settings.gemini_model,
            max_attempts=1,
        ),
    )
    observer = controlled_provider_observer(settings, provider, binding, session)
    provider._types = HostileTypes()
    request = ProviderGenerationRequest(
        system_instruction="",
        message="question",
        history=[],
        retrieved_context="context",
        max_output_tokens=10,
        max_output_chars=100,
    )
    with pytest.raises(RequestAccountingError):
        async for _ in provider.stream(request, observer=observer):
            pass
    assert session.finalize("error").attempts == ()


def _settled(identity: AttemptIdentity, completion: str) -> SettledAttempt:
    record = price_provider_attempt(
        ProviderAttemptAccountingInput(
            provider=identity.provider,
            model=identity.model,
            provider_attempt=identity.provider_attempt,
            attempt_date=ATTEMPT_DATE,
            completion_state=cast(Any, completion),
            answer_outcome="unverified",
        )
    )
    return SettledAttempt(identity=identity, cost_record=record)


def test_two_attempt_summary_requires_settled_error_predecessor() -> None:
    first = _identity(1)
    second = _identity(2)
    for invalid_first in (
        _settled(first, "completed"),
        UncertainAttempt(identity=first, reason="finish_missing"),
    ):
        with pytest.raises(RequestAccountingError):
            RequestAccountingSummary(
                provider_work_started=True,
                request_completion="completed",
                attempts=(invalid_first, _settled(second, "completed")),
            )

    valid = RequestAccountingSummary(
        provider_work_started=True,
        request_completion="completed",
        attempts=(_settled(first, "error"), _settled(second, "completed")),
    )
    assert tuple(attempt.identity.provider_attempt for attempt in valid.attempts) == (1, 2)
