"""Pure, content-free provider usage cost calculations."""

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date
from decimal import Context, Decimal, localcontext
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from app.providers.contracts import (
    MAX_RATE_DECIMAL_PLACES,
    MAX_RATE_TOTAL_DIGITS,
    MAX_SAFE_TOKEN_COUNT,
    PRICE_SOURCE_URL_MAX_CHARS,
    ProviderUsage,
    validate_model_identifier,
    validate_provider_identifier,
    validate_service_tier,
)

_SNAPSHOT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_DECIMAL_CONTEXT = Context(prec=80)


class ProviderAccountingError(Exception):
    """Content-free invalid accounting input."""

    def __init__(self) -> None:
        super().__init__("Provider accounting input is invalid.")

    def __repr__(self) -> str:
        return "ProviderAccountingError()"

    def errors(
        self,
        *,
        include_url: bool = True,
        include_context: bool = True,
        include_input: bool = True,
    ) -> list[dict[str, object]]:
        del include_url, include_context, include_input
        return [
            {
                "type": "provider_accounting_invalid",
                "loc": (),
                "msg": "Provider accounting input is invalid.",
            }
        ]

    def json(
        self,
        *,
        indent: int | None = None,
        include_url: bool = True,
        include_context: bool = True,
        include_input: bool = True,
    ) -> str:
        return json.dumps(
            self.errors(
                include_url=include_url,
                include_context=include_context,
                include_input=include_input,
            ),
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


class _ContentFreeAccountingValidator:
    def __init__(self, validator: Any) -> None:
        self._validator = validator

    def _validate(
        self,
        method_name: Literal["validate_python", "validate_json", "validate_strings"],
        value: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        kwargs["extra"] = "forbid"
        validated: Any = None
        failed = False
        try:
            validated = getattr(self._validator, method_name)(
                value,
                *args,
                **kwargs,
            )
        except ValidationError:
            failed = True
        if failed:
            raise ProviderAccountingError from None
        return validated

    def validate_python(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        return self._validate("validate_python", value, *args, **kwargs)

    def validate_json(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        return self._validate("validate_json", value, *args, **kwargs)

    def validate_strings(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        return self._validate("validate_strings", value, *args, **kwargs)

    def validate_assignment(self, *args: Any, **kwargs: Any) -> Any:
        validated: Any = None
        failed = False
        try:
            validated = self._validator.validate_assignment(*args, **kwargs)
        except ValidationError:
            failed = True
        if failed:
            raise ProviderAccountingError from None
        return validated

    def __getattr__(self, name: str) -> Any:
        return getattr(self._validator, name)


class ProviderAccountingModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _ContentFreeAccountingValidator):
            cls.__pydantic_validator__ = _ContentFreeAccountingValidator(validator)  # type: ignore[assignment]

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise ProviderAccountingError from None

    def __delattr__(self, name: str) -> None:
        del name
        raise ProviderAccountingError from None


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _validate_rate(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError("rate must be a Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError("rate must be finite and nonnegative")
    sign, digits, exponent = value.as_tuple()
    del sign
    assert isinstance(exponent, int)
    decimal_places = max(-exponent, 0)
    total_digits = len(digits) + max(exponent, 0)
    if (
        total_digits > MAX_RATE_TOTAL_DIGITS
        or decimal_places > MAX_RATE_DECIMAL_PLACES
    ):
        raise ValueError("rate exceeds decimal bounds")
    return value


def _validate_currency(value: str) -> str:
    if _CURRENCY_PATTERN.fullmatch(value) is None:
        raise ValueError("currency is invalid")
    return value


def _validate_source_url(value: str) -> str:
    if (
        not 1 <= len(value) <= PRICE_SOURCE_URL_MAX_CHARS
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("source URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("source URL is invalid")
    return value


def _snapshot_material(
    *,
    provider: str,
    model: str,
    service_tier: str | None,
    currency: str,
    effective_from: date,
    effective_through: date | None,
    source_url: str,
    uncached_input_rate_per_million: Decimal,
    cached_input_rate_per_million: Decimal,
    output_rate_per_million: Decimal,
    thinking_rate_per_million: Decimal,
) -> bytes:
    validate_provider_identifier(provider)
    validate_model_identifier(model)
    if service_tier is not None:
        validate_service_tier(service_tier)
    _validate_currency(currency)
    if effective_through is not None and effective_through < effective_from:
        raise ValueError("effective interval is invalid")
    _validate_source_url(source_url)
    rates = (
        _validate_rate(uncached_input_rate_per_million),
        _validate_rate(cached_input_rate_per_million),
        _validate_rate(output_rate_per_million),
        _validate_rate(thinking_rate_per_million),
    )
    material = {
        "schema_version": "1.0",
        "provider": provider,
        "model": model,
        "service_tier": service_tier,
        "currency": currency,
        "effective_from": effective_from.isoformat(),
        "effective_through": (
            None if effective_through is None else effective_through.isoformat()
        ),
        "source_url": source_url,
        "uncached_input_rate_per_million": _canonical_decimal(rates[0]),
        "cached_input_rate_per_million": _canonical_decimal(rates[1]),
        "output_rate_per_million": _canonical_decimal(rates[2]),
        "thinking_rate_per_million": _canonical_decimal(rates[3]),
    }
    return json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_price_snapshot_id(
    *,
    provider: str,
    model: str,
    service_tier: str | None,
    currency: str,
    effective_from: date,
    effective_through: date | None,
    source_url: str,
    uncached_input_rate_per_million: Decimal,
    cached_input_rate_per_million: Decimal,
    output_rate_per_million: Decimal,
    thinking_rate_per_million: Decimal,
) -> str:
    """Return the identity of one fully validated caller-supplied snapshot."""

    material: bytes | None = None
    try:
        material = _snapshot_material(
            provider=provider,
            model=model,
            service_tier=service_tier,
            currency=currency,
            effective_from=effective_from,
            effective_through=effective_through,
            source_url=source_url,
            uncached_input_rate_per_million=uncached_input_rate_per_million,
            cached_input_rate_per_million=cached_input_rate_per_million,
            output_rate_per_million=output_rate_per_million,
            thinking_rate_per_million=thinking_rate_per_million,
        )
    except (AttributeError, TypeError, ValueError):
        pass
    if material is None:
        raise ProviderAccountingError from None
    return hashlib.sha256(material).hexdigest()


class ProviderPriceSnapshot(ProviderAccountingModel):
    schema_version: Literal["1.0"] = "1.0"
    snapshot_id: str
    provider: str
    model: str
    service_tier: str | None = None
    currency: str
    effective_from: date
    effective_through: date | None = None
    source_url: str
    uncached_input_rate_per_million: Decimal
    cached_input_rate_per_million: Decimal
    output_rate_per_million: Decimal
    thinking_rate_per_million: Decimal

    @field_validator("snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, value: str) -> str:
        if _SNAPSHOT_PATTERN.fullmatch(value) is None:
            raise ValueError("snapshot ID is invalid")
        return value

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return validate_provider_identifier(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return validate_model_identifier(value)

    @field_validator("service_tier")
    @classmethod
    def validate_tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_service_tier(value)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        return _validate_currency(value)

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_source_url(value)

    @field_validator(
        "uncached_input_rate_per_million",
        "cached_input_rate_per_million",
        "output_rate_per_million",
        "thinking_rate_per_million",
        mode="before",
    )
    @classmethod
    def validate_rate(cls, value: object, info: ValidationInfo) -> Decimal:
        if info.mode == "json" and isinstance(value, str):
            try:
                value = Decimal(value)
            except Exception:
                raise ValueError("rate must be a Decimal") from None
        return _validate_rate(value)

    @model_validator(mode="after")
    def validate_identity(self) -> "ProviderPriceSnapshot":
        if (
            self.effective_through is not None
            and self.effective_through < self.effective_from
        ):
            raise ValueError("effective interval is invalid")
        expected = compute_price_snapshot_id(
            provider=self.provider,
            model=self.model,
            service_tier=self.service_tier,
            currency=self.currency,
            effective_from=self.effective_from,
            effective_through=self.effective_through,
            source_url=self.source_url,
            uncached_input_rate_per_million=self.uncached_input_rate_per_million,
            cached_input_rate_per_million=self.cached_input_rate_per_million,
            output_rate_per_million=self.output_rate_per_million,
            thinking_rate_per_million=self.thinking_rate_per_million,
        )
        if self.snapshot_id != expected:
            raise ValueError("snapshot identity is invalid")
        return self

    @field_serializer(
        "uncached_input_rate_per_million",
        "cached_input_rate_per_million",
        "output_rate_per_million",
        "thinking_rate_per_million",
        when_used="json",
    )
    def serialize_rate(self, value: Decimal) -> str:
        return _canonical_decimal(value)


class ProviderAttemptAccountingInput(ProviderAccountingModel):
    schema_version: Literal["1.0"] = "1.0"
    provider: str
    model: str
    service_tier: str | None = None
    provider_attempt: Annotated[StrictInt, Field(ge=1, le=2)]
    attempt_date: date
    completion_state: Literal["completed", "error", "cancelled"]
    answer_outcome: Literal["grounded", "refused", "unverified"] = "unverified"
    usage: ProviderUsage | None = None
    price_snapshot: ProviderPriceSnapshot | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return validate_provider_identifier(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return validate_model_identifier(value)

    @field_validator("service_tier")
    @classmethod
    def validate_tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_service_tier(value)

    @model_validator(mode="after")
    def validate_outcome(self) -> "ProviderAttemptAccountingInput":
        if self.completion_state != "completed" and self.answer_outcome != "unverified":
            raise ValueError("incomplete attempt outcome is invalid")
        return self


class ProviderAttemptCostRecord(ProviderAccountingModel):
    schema_version: Literal["1.0"] = "1.0"
    provider: str
    model: str
    service_tier: str | None = None
    provider_attempt: Annotated[StrictInt, Field(ge=1, le=2)]
    attempt_date: date
    completion_state: Literal["completed", "error", "cancelled"]
    answer_outcome: Literal["grounded", "refused", "unverified"]
    usage: ProviderUsage | None
    snapshot_id: str | None
    currency: str | None
    model_cost: Decimal | None
    cost_state: Literal[
        "priced",
        "usage_missing",
        "usage_incomplete",
        "snapshot_missing",
    ]

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return validate_provider_identifier(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return validate_model_identifier(value)

    @field_validator("service_tier")
    @classmethod
    def validate_tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_service_tier(value)

    @field_validator("snapshot_id")
    @classmethod
    def validate_optional_snapshot_id(cls, value: str | None) -> str | None:
        if value is not None and _SNAPSHOT_PATTERN.fullmatch(value) is None:
            raise ValueError("snapshot ID is invalid")
        return value

    @field_validator("currency")
    @classmethod
    def validate_optional_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_currency(value)

    @field_validator("model_cost", mode="before")
    @classmethod
    def validate_cost(cls, value: object, info: ValidationInfo) -> Decimal | None:
        if value is None:
            return None
        if info.mode == "json" and isinstance(value, str):
            try:
                value = Decimal(value)
            except Exception:
                raise ValueError("model cost is invalid") from None
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("model cost is invalid")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> "ProviderAttemptCostRecord":
        priced_fields = (self.snapshot_id, self.currency, self.model_cost)
        if self.cost_state == "priced":
            if (
                any(value is None for value in priced_fields)
                or self.usage is None
                or not _complete_usage(self.usage)
            ):
                raise ValueError("priced record is incomplete")
        elif any(value is not None for value in priced_fields):
            raise ValueError("unknown cost must not contain price values")
        elif self.cost_state == "usage_missing" and self.usage is not None:
            raise ValueError("usage-missing record contains usage")
        elif self.cost_state == "usage_incomplete" and (
            self.usage is None or _complete_usage(self.usage)
        ):
            raise ValueError("usage-incomplete record is inconsistent")
        elif self.cost_state == "snapshot_missing" and (
            self.usage is None or not _complete_usage(self.usage)
        ):
            raise ValueError("snapshot-missing record is inconsistent")
        return self

    @field_serializer("model_cost", when_used="json")
    def serialize_cost(self, value: Decimal | None) -> str | None:
        return None if value is None else _canonical_decimal(value)


def _complete_usage(usage: ProviderUsage) -> bool:
    return all(
        getattr(usage, field_name) is not None
        for field_name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "thinking_tokens",
        )
    )


def _attempt_record(
    attempt: ProviderAttemptAccountingInput,
    *,
    snapshot_id: str | None,
    currency: str | None,
    model_cost: Decimal | None,
    cost_state: Literal[
        "priced", "usage_missing", "usage_incomplete", "snapshot_missing"
    ],
) -> ProviderAttemptCostRecord:
    return ProviderAttemptCostRecord(
        provider=attempt.provider,
        model=attempt.model,
        service_tier=attempt.service_tier,
        provider_attempt=attempt.provider_attempt,
        attempt_date=attempt.attempt_date,
        completion_state=attempt.completion_state,
        answer_outcome=attempt.answer_outcome,
        usage=attempt.usage,
        snapshot_id=snapshot_id,
        currency=currency,
        model_cost=model_cost,
        cost_state=cost_state,
    )


def price_provider_attempt(
    attempt: ProviderAttemptAccountingInput,
) -> ProviderAttemptCostRecord:
    """Apply one explicit price snapshot without lookup or fallback."""

    if attempt.usage is None:
        return _attempt_record(
            attempt,
            snapshot_id=None,
            currency=None,
            model_cost=None,
            cost_state="usage_missing",
        )
    if not _complete_usage(attempt.usage):
        return _attempt_record(
            attempt,
            snapshot_id=None,
            currency=None,
            model_cost=None,
            cost_state="usage_incomplete",
        )
    if attempt.price_snapshot is None:
        return _attempt_record(
            attempt,
            snapshot_id=None,
            currency=None,
            model_cost=None,
            cost_state="snapshot_missing",
        )

    snapshot = attempt.price_snapshot
    if (
        snapshot.provider != attempt.provider
        or snapshot.model != attempt.model
        or snapshot.service_tier != attempt.service_tier
        or attempt.attempt_date < snapshot.effective_from
        or (
            snapshot.effective_through is not None
            and attempt.attempt_date > snapshot.effective_through
        )
    ):
        raise ProviderAccountingError from None

    usage = attempt.usage
    assert usage.input_tokens is not None
    assert usage.cached_input_tokens is not None
    assert usage.output_tokens is not None
    assert usage.thinking_tokens is not None
    uncached_input = usage.input_tokens - usage.cached_input_tokens
    with localcontext(_DECIMAL_CONTEXT):
        cost = (
            Decimal(uncached_input) * snapshot.uncached_input_rate_per_million
            + Decimal(usage.cached_input_tokens)
            * snapshot.cached_input_rate_per_million
            + Decimal(usage.output_tokens) * snapshot.output_rate_per_million
            + Decimal(usage.thinking_tokens) * snapshot.thinking_rate_per_million
        ) / Decimal(1_000_000)
    return _attempt_record(
        attempt,
        snapshot_id=snapshot.snapshot_id,
        currency=snapshot.currency,
        model_cost=cost,
        cost_state="priced",
    )


class ProviderCostAggregate(ProviderAccountingModel):
    schema_version: Literal["1.0"] = "1.0"
    target_attempt_count: Annotated[
        StrictInt, Field(ge=1, le=MAX_SAFE_TOKEN_COUNT)
    ]
    attempted_count: Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_TOKEN_COUNT)]
    priced_count: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)]
    grounded_count: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)]
    priced_coverage: Decimal
    average_priced_model_cost: Decimal | None
    projected_model_cost: Decimal | None
    model_cost_per_grounded_answer: Decimal | None
    currency: str | None

    @field_validator(
        "priced_coverage",
        "average_priced_model_cost",
        "projected_model_cost",
        "model_cost_per_grounded_answer",
        mode="before",
    )
    @classmethod
    def validate_decimal(cls, value: object, info: ValidationInfo) -> Decimal | None:
        if value is None:
            return None
        if info.mode == "json" and isinstance(value, str):
            try:
                value = Decimal(value)
            except Exception:
                raise ValueError("aggregate decimal is invalid") from None
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("aggregate decimal is invalid")
        return value

    @field_validator("currency")
    @classmethod
    def validate_optional_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_currency(value)

    @model_validator(mode="after")
    def validate_counts(self) -> "ProviderCostAggregate":
        if self.priced_count > self.attempted_count:
            raise ValueError("priced count is invalid")
        if self.grounded_count > self.attempted_count:
            raise ValueError("grounded count is invalid")
        with localcontext(_DECIMAL_CONTEXT):
            expected_coverage = Decimal(self.priced_count) / Decimal(
                self.attempted_count
            )
        if self.priced_coverage != expected_coverage:
            raise ValueError("priced coverage is invalid")
        if self.priced_count == 0:
            if self.average_priced_model_cost is not None or self.currency is not None:
                raise ValueError("unpriced aggregate contains price values")
        elif self.average_priced_model_cost is None or self.currency is None:
            raise ValueError("priced aggregate is incomplete")
        full_coverage = self.priced_count == self.attempted_count
        if full_coverage:
            if self.projected_model_cost is None:
                raise ValueError("full-coverage projection is missing")
        elif (
            self.projected_model_cost is not None
            or self.model_cost_per_grounded_answer is not None
        ):
            raise ValueError("partial-coverage aggregate contains derived cost")
        if self.grounded_count == 0:
            if self.model_cost_per_grounded_answer is not None:
                raise ValueError("zero-grounded aggregate contains derived cost")
        elif full_coverage and self.model_cost_per_grounded_answer is None:
            raise ValueError("grounded cost is missing")
        return self

    @field_serializer(
        "priced_coverage",
        "average_priced_model_cost",
        "projected_model_cost",
        "model_cost_per_grounded_answer",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return None if value is None else _canonical_decimal(value)


def aggregate_provider_costs(
    records: Sequence[ProviderAttemptCostRecord],
    *,
    target_attempt_count: int,
) -> ProviderCostAggregate:
    """Aggregate explicit attempt records with coverage-aware projections."""

    if (
        not records
        or isinstance(target_attempt_count, bool)
        or not 1 <= target_attempt_count <= MAX_SAFE_TOKEN_COUNT
        or len(records) > MAX_SAFE_TOKEN_COUNT
    ):
        raise ProviderAccountingError from None
    priced = [record for record in records if record.cost_state == "priced"]
    currencies = {record.currency for record in priced}
    if len(currencies) > 1:
        raise ProviderAccountingError from None
    currency = next(iter(currencies), None)
    costs = [record.model_cost for record in priced]
    if any(cost is None for cost in costs):
        raise ProviderAccountingError from None
    exact_costs = [cost for cost in costs if cost is not None]
    grounded_count = sum(record.answer_outcome == "grounded" for record in records)
    with localcontext(_DECIMAL_CONTEXT):
        total = sum(exact_costs, start=Decimal(0))
        attempted_count = len(records)
        priced_count = len(priced)
        coverage = Decimal(priced_count) / Decimal(attempted_count)
        average = None if not priced else total / Decimal(priced_count)
        full_coverage = priced_count == attempted_count
        projection = (
            None
            if average is None or not full_coverage
            else average * Decimal(target_attempt_count)
        )
        per_grounded = (
            None
            if not full_coverage or grounded_count == 0
            else total / Decimal(grounded_count)
        )
    return ProviderCostAggregate(
        target_attempt_count=target_attempt_count,
        attempted_count=attempted_count,
        priced_count=priced_count,
        grounded_count=grounded_count,
        priced_coverage=coverage,
        average_priced_model_cost=average,
        projected_model_cost=projection,
        model_cost_per_grounded_answer=per_grounded,
        currency=currency,
    )
