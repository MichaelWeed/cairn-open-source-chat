import json
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from app.providers.accounting import (
    ProviderAccountingError,
    ProviderAttemptAccountingInput,
    ProviderAttemptCostRecord,
    ProviderCostAggregate,
    ProviderPriceSnapshot,
    aggregate_provider_costs,
    compute_price_snapshot_id,
    price_provider_attempt,
)
from app.providers.contracts import (
    MAX_SAFE_TOKEN_COUNT,
    ProviderStreamEvent,
    ProviderTextChunk,
    ProviderUsage,
    ProviderUsageChunk,
    ProviderUsageValidationError,
    merge_cumulative_usage,
)


def _usage(**changes: object) -> ProviderUsage:
    values: dict[str, object] = {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "thinking_tokens": 10,
        "total_tokens": 140,
    }
    values.update(changes)
    return ProviderUsage.model_validate(values)


def _usage_event(**changes: object) -> ProviderUsageChunk:
    values: dict[str, object] = {
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "provider_attempt": 1,
        "service_tier": None,
        "usage": _usage(),
    }
    values.update(changes)
    return ProviderUsageChunk.model_validate(values)


def _snapshot(**changes: object) -> ProviderPriceSnapshot:
    values: dict[str, object] = {
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "service_tier": None,
        "currency": "USD",
        "effective_from": date(2026, 1, 1),
        "effective_through": None,
        "source_url": "https://example.com/pricing",
        "uncached_input_rate_per_million": Decimal("1.25"),
        "cached_input_rate_per_million": Decimal("0.25"),
        "output_rate_per_million": Decimal("5"),
        "thinking_rate_per_million": Decimal("2.50"),
    }
    values.update(changes)
    values["snapshot_id"] = compute_price_snapshot_id(**cast(Any, values))
    return ProviderPriceSnapshot.model_validate(values)


def _attempt(**changes: object) -> ProviderAttemptAccountingInput:
    values: dict[str, object] = {
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "service_tier": None,
        "provider_attempt": 1,
        "attempt_date": date(2026, 6, 1),
        "completion_state": "completed",
        "answer_outcome": "grounded",
        "usage": _usage(),
        "price_snapshot": _snapshot(),
    }
    values.update(changes)
    return ProviderAttemptAccountingInput.model_validate(values)


def test_provider_event_union_is_strict_discriminated_and_bounded() -> None:
    adapter: TypeAdapter[ProviderStreamEvent] = TypeAdapter(ProviderStreamEvent)
    text = adapter.validate_python({"kind": "text", "schema_version": "1.0", "delta": "ok"})
    usage = adapter.validate_python(
        {
            "kind": "usage",
            "schema_version": "1.0",
            "provider": "ollama",
            "model": "llama3.1:8b-instruct",
            "provider_attempt": 1,
            "service_tier": None,
            "usage": {"input_tokens": 0},
        }
    )

    assert isinstance(text, ProviderTextChunk)
    assert isinstance(usage, ProviderUsageChunk)
    assert text.model_dump() == {"kind": "text", "schema_version": "1.0", "delta": "ok"}
    assert usage.usage.input_tokens == 0

    invalid_values = [
        {"schema_version": "1.0", "delta": "missing kind"},
        {"kind": "unknown", "schema_version": "1.0", "delta": "x"},
        {"kind": "text", "schema_version": "1.0", "delta": ""},
        {"kind": "text", "schema_version": "1.0", "delta": "x" * 1001},
        {"kind": "text", "schema_version": "1.0", "delta": "x", "extra": True},
    ]
    for value in invalid_values:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


@pytest.mark.parametrize("bad", [True, -1, MAX_SAFE_TOKEN_COUNT + 1])
def test_usage_rejects_invalid_counts(bad: object) -> None:
    with pytest.raises(ValidationError):
        ProviderUsage(input_tokens=bad)  # type: ignore[arg-type]


def test_usage_requires_one_value_and_consistent_subsets_and_total() -> None:
    with pytest.raises(ValidationError):
        ProviderUsage()
    with pytest.raises(ValidationError):
        ProviderUsage(input_tokens=1, cached_input_tokens=2)
    with pytest.raises(ValidationError):
        ProviderUsage(
            input_tokens=1,
            output_tokens=2,
            thinking_tokens=3,
            total_tokens=7,
        )

    assert ProviderUsage(input_tokens=0).model_dump()["input_tokens"] == 0
    assert ProviderUsage(output_tokens=0).input_tokens is None


def test_cumulative_usage_merges_without_summing_and_preserves_identity() -> None:
    first = _usage_event(
        usage=ProviderUsage(input_tokens=100, output_tokens=10),
    )
    second = _usage_event(
        service_tier="priority",
        usage=ProviderUsage(input_tokens=100, output_tokens=12, total_tokens=112),
    )

    merged = merge_cumulative_usage(first, second)

    assert merged.service_tier == "priority"
    assert merged.usage == ProviderUsage(
        input_tokens=100,
        output_tokens=12,
        total_tokens=112,
    )

    assert merge_cumulative_usage(merged, _usage_event(
        service_tier=None,
        usage=ProviderUsage(input_tokens=100, output_tokens=12),
    )).service_tier == "priority"

    invalid_updates = [
        _usage_event(provider="ollama"),
        _usage_event(model="other"),
        _usage_event(provider_attempt=2),
        _usage_event(service_tier="standard"),
        _usage_event(usage=ProviderUsage(input_tokens=99)),
    ]
    for update in invalid_updates:
        with pytest.raises(ProviderUsageValidationError) as caught:
            merge_cumulative_usage(merged, update)
        assert str(caught.value) == "Provider usage metadata is invalid."


def test_snapshot_id_is_canonical_and_json_money_is_exact() -> None:
    snapshot = _snapshot(
        model="modèle-v1",
        uncached_input_rate_per_million=Decimal("1.2500"),
        thinking_rate_per_million=Decimal("-0.000"),
    )

    assert snapshot.snapshot_id == (
        "4c543c73d479afea55f0ac85d138bfd8438fece50a8f59089dc20ec38aa90b5d"
    )
    payload = snapshot.model_dump_json()
    assert '"uncached_input_rate_per_million":"1.25"' in payload
    assert '"thinking_rate_per_million":"0"' in payload
    assert '"service_tier":null' in payload
    assert '"effective_through":null' in payload
    assert "modèle-v1" in payload


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "usd"},
        {"effective_through": date(2025, 12, 31)},
        {"source_url": "http://example.com/pricing"},
        {"source_url": "https://user@example.com/pricing"},
        {"source_url": "https://example.com/pricing?tracked=1"},
        {"source_url": "https://example.com/pricing#fragment"},
        {"source_url": "https://example.com/price path"},
        {"output_rate_per_million": Decimal("NaN")},
        {"output_rate_per_million": Decimal("Infinity")},
        {"output_rate_per_million": Decimal("-0.01")},
        {"output_rate_per_million": Decimal("0.0000000000000000001")},
        {"output_rate_per_million": Decimal("1E+38")},
        {"output_rate_per_million": 1.5},
    ],
)
def test_snapshot_rejects_invalid_currency_interval_url_and_rates(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ProviderAccountingError):
        _snapshot(**changes)


def test_snapshot_rejects_stale_identity_after_any_field_change() -> None:
    snapshot = _snapshot()
    values = snapshot.model_dump()
    values["output_rate_per_million"] = Decimal("6")

    with pytest.raises(ProviderAccountingError):
        ProviderPriceSnapshot.model_validate(values)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("provider", "ollama"),
        ("model", "another-model"),
        ("service_tier", "priority"),
        ("currency", "EUR"),
        ("effective_from", date(2026, 1, 2)),
        ("effective_through", date(2026, 12, 31)),
        ("source_url", "https://example.com/new-pricing"),
        ("uncached_input_rate_per_million", Decimal("1.5")),
        ("cached_input_rate_per_million", Decimal("0.5")),
        ("output_rate_per_million", Decimal("6")),
        ("thinking_rate_per_million", Decimal("3")),
    ],
)
def test_snapshot_identity_covers_every_economic_and_provenance_field(
    field_name: str, replacement: object
) -> None:
    values = _snapshot().model_dump()
    values[field_name] = replacement

    with pytest.raises(ProviderAccountingError):
        ProviderPriceSnapshot.model_validate(values)


def test_snapshot_is_frozen_and_round_trips_exact_json() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError):
        snapshot.currency = "EUR"
    assert ProviderPriceSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_derived_decimal_models_round_trip_exact_json() -> None:
    record = price_provider_attempt(_attempt())
    aggregate = aggregate_provider_costs((record,), target_attempt_count=3)

    assert ProviderAttemptCostRecord.model_validate_json(record.model_dump_json()) == record
    assert ProviderCostAggregate.model_validate_json(aggregate.model_dump_json()) == aggregate


def test_attempt_cost_uses_exact_decimal_buckets() -> None:
    record = price_provider_attempt(_attempt())

    assert record.cost_state == "priced"
    assert record.model_cost == Decimal("0.00028")
    assert record.snapshot_id == _snapshot().snapshot_id
    assert json.loads(record.model_dump_json())["model_cost"] == "0.00028"


def test_explicit_zero_usage_and_rates_produce_known_zero_cost() -> None:
    zero_usage = ProviderUsage(
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        total_tokens=0,
    )
    zero_snapshot = _snapshot(
        uncached_input_rate_per_million=Decimal("0"),
        cached_input_rate_per_million=Decimal("0"),
        output_rate_per_million=Decimal("0"),
        thinking_rate_per_million=Decimal("0"),
    )

    record = price_provider_attempt(
        _attempt(usage=zero_usage, price_snapshot=zero_snapshot)
    )

    assert record.cost_state == "priced"
    assert record.model_cost == Decimal("0")


@pytest.mark.parametrize(
    ("changes", "expected_state"),
    [
        ({"usage": None}, "usage_missing"),
        ({"usage": ProviderUsage(input_tokens=10)}, "usage_incomplete"),
        ({"price_snapshot": None}, "snapshot_missing"),
    ],
)
def test_missing_evidence_is_unknown_not_zero(
    changes: dict[str, object], expected_state: str
) -> None:
    record = price_provider_attempt(_attempt(**changes))

    assert record.cost_state == expected_state
    assert record.model_cost is None
    assert record.snapshot_id is None
    assert record.currency is None


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "ollama"},
        {"model": "other"},
        {"service_tier": "priority"},
        {"attempt_date": date(2025, 12, 31)},
    ],
)
def test_supplied_snapshot_mismatch_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ProviderAccountingError, match="Provider accounting input is invalid"):
        price_provider_attempt(_attempt(**changes))


def test_attempt_state_rejects_grounded_error_or_cancelled() -> None:
    for state in ("error", "cancelled"):
        with pytest.raises(ProviderAccountingError):
            _attempt(completion_state=state, answer_outcome="grounded")


def test_aggregate_coverage_controls_projection_and_grounded_denominator() -> None:
    priced = price_provider_attempt(_attempt())
    unknown = price_provider_attempt(
        _attempt(
            provider_attempt=2,
            answer_outcome="unverified",
            completion_state="cancelled",
            usage=None,
            price_snapshot=None,
        )
    )

    partial = aggregate_provider_costs((priced, unknown), target_attempt_count=1_000)
    assert partial.attempted_count == 2
    assert partial.priced_count == 1
    assert partial.grounded_count == 1
    assert partial.priced_coverage == Decimal("0.5")
    assert partial.average_priced_model_cost == Decimal("0.00028")
    assert partial.projected_model_cost is None
    assert partial.model_cost_per_grounded_answer is None

    full = aggregate_provider_costs((priced,), target_attempt_count=1_000)
    assert full.priced_coverage == Decimal("1")
    assert full.projected_model_cost == Decimal("0.28")
    assert full.model_cost_per_grounded_answer == Decimal("0.00028")


def test_aggregate_rejects_empty_bad_target_and_mixed_currency() -> None:
    with pytest.raises(ProviderAccountingError, match="Provider accounting input is invalid"):
        aggregate_provider_costs((), target_attempt_count=1)
    with pytest.raises(ProviderAccountingError, match="Provider accounting input is invalid"):
        aggregate_provider_costs((price_provider_attempt(_attempt()),), target_attempt_count=0)

    usd = price_provider_attempt(_attempt())
    eur_snapshot = _snapshot(currency="EUR")
    eur = price_provider_attempt(_attempt(price_snapshot=eur_snapshot))
    with pytest.raises(ProviderAccountingError, match="Provider accounting input is invalid"):
        aggregate_provider_costs((usd, eur), target_attempt_count=2)


def test_full_coverage_with_no_grounded_answer_keeps_denominator_unknown() -> None:
    error = price_provider_attempt(
        _attempt(completion_state="error", answer_outcome="unverified")
    )
    cancelled = price_provider_attempt(
        _attempt(
            provider_attempt=2,
            completion_state="cancelled",
            answer_outcome="unverified",
        )
    )

    aggregate = aggregate_provider_costs((error, cancelled), target_attempt_count=2)

    assert aggregate.attempted_count == 2
    assert aggregate.priced_count == 2
    assert aggregate.grounded_count == 0
    assert aggregate.projected_model_cost is not None
    assert aggregate.model_cost_per_grounded_answer is None


def test_derived_models_reject_inconsistent_states() -> None:
    priced = price_provider_attempt(_attempt())
    record_values = priced.model_dump()
    record_values["usage"] = None
    with pytest.raises(ProviderAccountingError):
        ProviderAttemptCostRecord.model_validate(record_values)

    aggregate = aggregate_provider_costs((priced,), target_attempt_count=1)
    aggregate_values = aggregate.model_dump()
    aggregate_values["priced_coverage"] = Decimal("0.5")
    with pytest.raises(ProviderAccountingError):
        ProviderCostAggregate.model_validate(aggregate_values)


def test_accounting_models_reject_content_and_serialize_no_forbidden_fields() -> None:
    with pytest.raises(ProviderAccountingError):
        ProviderAttemptAccountingInput.model_validate(
            {**_attempt().model_dump(), "message": "prompt-sentinel"}
        )

    record = price_provider_attempt(_attempt())
    aggregate = aggregate_provider_costs((record,), target_attempt_count=1)
    serialized = record.model_dump_json() + aggregate.model_dump_json()
    for forbidden in (
        "prompt-sentinel",
        "session_id",
        "response_id",
        "api_key",
        "source_url",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("entrypoint", ["constructor", "python", "json", "strings"])
def test_untrusted_accounting_validation_errors_are_content_free(
    entrypoint: str,
) -> None:
    secret = "prompt-secret-sentinel"
    payload = {**_attempt().model_dump(), "message": secret}

    with pytest.raises(ProviderAccountingError) as caught:
        if entrypoint == "constructor":
            ProviderAttemptAccountingInput(**payload)
        elif entrypoint == "python":
            ProviderAttemptAccountingInput.model_validate(payload)
        elif entrypoint == "json":
            ProviderAttemptAccountingInput.model_validate_json(
                json.dumps(payload, default=str)
            )
        else:
            ProviderAttemptAccountingInput.model_validate_strings(
                {"message": secret}
            )

    rendered = (
        str(caught.value)
        + json.dumps(caught.value.errors(include_input=True))
        + caught.value.json(include_input=True)
    )
    assert rendered.count("Provider accounting input is invalid.") == 3
    assert secret not in rendered
    assert "message" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_sensitive_snapshot_values_do_not_survive_structured_validation_error() -> None:
    source_secret = "source-secret-sentinel"
    rate_secret = 987654.125
    payload = _snapshot().model_dump()
    payload["source_url"] = f"https://example.com/pricing?key={source_secret}"
    payload["output_rate_per_million"] = rate_secret

    with pytest.raises(ProviderAccountingError) as caught:
        ProviderPriceSnapshot.model_validate(payload)

    rendered = (
        str(caught.value)
        + json.dumps(caught.value.errors(include_input=True))
        + caught.value.json(include_input=True)
    )
    assert source_secret not in rendered
    assert str(rate_secret) not in rendered
    assert "source_url" not in rendered
    assert "output_rate_per_million" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
