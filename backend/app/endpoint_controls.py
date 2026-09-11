"""Deployment-wide public endpoint admission and lease controls.

The store protocol is deliberately vendor-neutral and borrowed: the controller
never owns or closes an injected store.  ``InMemoryEndpointControlStore`` is a
deterministic conformance implementation for development and tests, not a
production shared-store adapter.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import ipaddress
import json
import math
import re
import secrets
import time
import types
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from functools import wraps
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic import SecretStr

from app.request_accounting import RequestAccountingSummary, SettledAttempt

CONTROL_STORE_CALL_TIMEOUT_SECONDS = 5.0
CONTROL_QUEUE_POLL_INTERVAL_SECONDS = 1.0
CONTROL_MIN_LEASE_RENEW_SECONDS = 10.0
CONTROL_MAX_RENEWAL_TICKS_PER_LEASE = 60
CONTROL_RECEIPT_SCHEMA_VERSION = "1.0"
CONTROL_MAX_INTEGER = 2_147_483_647
CONTROL_MAX_XFF_BYTES = 1_024
CONTROL_MAX_XFF_HOPS = 16
CONTROL_MAX_TRUSTED_CIDRS = 32
CONTROL_MAX_HEADERS = 128
CONTROL_MAX_RECEIPT_JSON_BYTES = 4_096
CONTROL_MAX_CIDR_TEXT_BYTES = 2_048
_CONTROL_PREFIX = b"cairn-control-v1"
_HEX_32_LENGTH = 32
_HEX_64_LENGTH = 64
_MIN_INSTANT = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_BASE_INSTANT = datetime(9999, 11, 30, 23, 59, 59, 999999, tzinfo=UTC)
_MAX_INSTANT = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
_MONEY_MAX_DIGITS = 38
_MONEY_MAX_SCALE = 18
_CANONICAL_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_CONTROL_DECIMAL_CONTEXT = Context(prec=96, rounding=ROUND_HALF_EVEN)

ControlErrorCode = Literal[
    "store_capacity",
    "malformed_store",
    "store_conflict",
    "store_unavailable",
    "store_timeout",
    "unknown_outcome",
    "fencing_lost",
    "lease_expired",
    "controller_invariant",
]
_CONTROL_ERROR_CODES = frozenset(
    {
        "store_capacity",
        "malformed_store",
        "store_conflict",
        "store_unavailable",
        "store_timeout",
        "unknown_outcome",
        "fencing_lost",
        "lease_expired",
        "controller_invariant",
    }
)
DenialReason = Literal[
    "ip_rate_limited",
    "session_rate_limited",
    "budget_exhausted",
    "concurrency_limited",
]
OperationKind = Literal[
    "admit_or_enqueue",
    "wait_for_admission",
    "renew",
    "finalize",
    "cancel_ticket",
]


class _ContentFreeError(Exception):
    """An internal failure whose ordinary surfaces never retain input."""

    __slots__ = ()

    def __init__(self) -> None:
        BaseException.__init__(self)

    def __str__(self) -> str:
        return f"{type(self).__name__}()"

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def __getattribute__(self, name: str) -> Any:
        if name in {"__traceback__", "__cause__", "__context__"}:
            return None
        return BaseException.__getattribute__(self, name)

    def errors(self, **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [{"type": type(self).__name__, "loc": (), "msg": str(self)}]

    def json(self, **kwargs: object) -> str:
        indent = kwargs.get("indent")
        if indent is not None and type(indent) is not int:
            indent = None
        return json.dumps(
            self.errors(),
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


class ClientIdentityError(_ContentFreeError):
    pass


class ClientPeerError(ClientIdentityError):
    pass


class ControlConfigurationError(_ContentFreeError):
    pass


class ControlStoreError(_ContentFreeError):
    __slots__ = ("code",)
    code: ControlErrorCode

    def __init__(self, code: ControlErrorCode = "malformed_store") -> None:
        if type(code) is not str or code not in _CONTROL_ERROR_CODES:
            code = "malformed_store"
        BaseException.__init__(self)
        object.__setattr__(self, "code", code)


class EndpointControllerError(_ContentFreeError):
    pass


class _DetachedConfigMeta(type):
    def __call__(cls, *args: object, **kwargs: object) -> object:
        failed = False
        try:
            result = super().__call__(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as error:
            _release_exception(error)
            failed = True
        if failed:
            del args, kwargs
            raise ControlConfigurationError from None
        return result


class _DetachedStoreMeta(type):
    def __call__(cls, *args: object, **kwargs: object) -> object:
        failed = False
        try:
            result = super().__call__(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as error:
            _release_exception(error)
            failed = True
        if failed:
            del args, kwargs
            raise ControlStoreError("malformed_store") from None
        return result


def _normalized_store_error(value: object) -> ControlStoreError:
    if type(value) is not ControlStoreError:
        return ControlStoreError("malformed_store")
    raw = object.__getattribute__(value, "code")
    if type(raw) is not str or raw not in _CONTROL_ERROR_CODES:
        return ControlStoreError("malformed_store")
    return ControlStoreError(cast(ControlErrorCode, raw))


def _detached_store_call[**P, R](
    operation: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(operation)
    async def detached(*args: P.args, **kwargs: P.kwargs) -> R:
        code: ControlErrorCode = "malformed_store"
        failed = False
        primary: BaseException | None = None
        try:
            result = await operation(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as error:
            primary = error
        except BaseException as error:
            if type(error) is ControlStoreError:
                code = _normalized_store_error(error).code
            _release_exception(error)
            failed = True
        if primary is not None:
            exact_primary = primary
            del args, kwargs, primary
            raise exact_primary.with_traceback(None) from None
        if failed:
            del args, kwargs
            raise ControlStoreError(code) from None
        return result

    return detached


def _has_async_store_shape(value: object) -> bool:
    for name in (
        "admit_or_enqueue",
        "wait_for_admission",
        "renew",
        "finalize",
        "cancel_ticket",
        "check_budget_readiness",
    ):
        try:
            descriptor = inspect.getattr_static(value, name)
        except AttributeError:
            return False
        if type(descriptor) is not types.FunctionType or not inspect.iscoroutinefunction(
            descriptor
        ):
            return False
    return True


def _is_hex(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _checked_int(value: object, *, minimum: int = 1, maximum: int = CONTROL_MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ControlConfigurationError from None
    return value


def _strict_decimal(
    value: object,
    *,
    positive: bool = False,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    seconds: bool = False,
) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ControlConfigurationError from None
    if value.is_zero() and value.is_signed():
        raise ControlConfigurationError from None
    if positive and value <= 0:
        raise ControlConfigurationError from None
    if minimum is not None and value < minimum:
        raise ControlConfigurationError from None
    if maximum is not None and value > maximum:
        raise ControlConfigurationError from None
    sign, digits, raw_exponent = value.as_tuple()
    del sign
    exponent = cast(int, raw_exponent)
    scale = max(0, -exponent)
    total = len(digits) + exponent if exponent >= 0 else max(len(digits), scale)
    if total > _MONEY_MAX_DIGITS or scale > _MONEY_MAX_SCALE:
        raise ControlConfigurationError from None
    if seconds and scale > 6:
        raise ControlConfigurationError from None
    return value


def _decimal_seconds(value: Decimal) -> timedelta:
    with localcontext(_CONTROL_DECIMAL_CONTEXT):
        microseconds = value * Decimal(1_000_000)
    if microseconds != microseconds.to_integral_value():
        raise ControlStoreError("malformed_store") from None
    try:
        return timedelta(microseconds=int(microseconds))
    except (OverflowError, ValueError):
        raise ControlStoreError("malformed_store") from None


def _decimal_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CONTROL_DECIMAL_CONTEXT):
        return left + right


def _decimal_multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CONTROL_DECIMAL_CONTEXT):
        return left * right


def canonical_utc_instant(value: object) -> str:
    if type(value) is not datetime or value.tzinfo is not UTC:
        raise ControlStoreError("malformed_store") from None
    instant = value
    if not _MIN_INSTANT <= instant <= _MAX_INSTANT:
        raise ControlStoreError("malformed_store") from None
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_canonical_utc_instant(value: object) -> datetime:
    if type(value) is not str or len(value) != 27 or not value.isascii():
        raise ControlStoreError("malformed_store") from None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise ControlStoreError("malformed_store") from None
    if canonical_utc_instant(parsed) != value:
        raise ControlStoreError("malformed_store") from None
    return parsed


def _hour_identity(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:00:00Z")


def _day_identity(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%d")


def _canonical_ip(
    value: object,
    error_type: type[ClientIdentityError] = ClientIdentityError,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if type(value) is not str or not value or value != value.strip():
        raise error_type from None
    if any(character in value for character in ("[", "]", "%")):
        raise error_type from None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise error_type from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _networks(
    values: Sequence[str | ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if type(values) is not tuple or len(values) > CONTROL_MAX_TRUSTED_CIDRS:
        raise ControlConfigurationError from None
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    seen: set[str] = set()
    for raw in values:
        if type(raw) is str:
            if raw != raw.strip() or not raw:
                raise ControlConfigurationError from None
            try:
                network = ipaddress.ip_network(raw, strict=True)
            except ValueError:
                raise ControlConfigurationError from None
        elif type(raw) in {ipaddress.IPv4Network, ipaddress.IPv6Network}:
            network = raw
        else:
            raise ControlConfigurationError from None
        canonical = str(network)
        if canonical in seen:
            raise ControlConfigurationError from None
        seen.add(canonical)
        parsed.append(network)
    return tuple(parsed)


def _detached_identity_boundary[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    @wraps(operation)
    def detached(*args: P.args, **kwargs: P.kwargs) -> R:
        failure = "identity"
        primary: BaseException | None = None
        try:
            result = operation(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as error:
            primary = error
        except BaseException as error:
            if type(error) is ClientPeerError:
                failure = "peer"
            elif type(error) is ControlConfigurationError:
                failure = "config"
            _release_exception(error)
        else:
            return result
        if primary is not None:
            exact_primary = primary
            del args, kwargs, primary
            raise exact_primary.with_traceback(None) from None
        kind = failure
        del args, kwargs, failure
        if kind == "peer":
            raise ClientPeerError from None
        if kind == "config":
            raise ControlConfigurationError from None
        raise ClientIdentityError from None

    return detached


def _detached_configuration_boundary[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    @wraps(operation)
    def detached(*args: P.args, **kwargs: P.kwargs) -> R:
        primary: BaseException | None = None
        try:
            result = operation(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as error:
            primary = error
        except BaseException as error:
            _release_exception(error)
        else:
            return result
        if primary is not None:
            exact_primary = primary
            del args, kwargs, primary
            raise exact_primary.with_traceback(None) from None
        del args, kwargs
        raise ControlConfigurationError from None

    return detached


@_detached_identity_boundary
def resolve_client_identity(
    peer: object,
    headers: Iterable[tuple[bytes, bytes]],
    trusted_proxy_networks: Sequence[str | ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    """Resolve one canonical peer without trusting ambient proxy conventions."""

    immediate = _canonical_ip(peer, ClientPeerError)
    if type(trusted_proxy_networks) is not tuple:
        raise ControlConfigurationError from None
    networks = _networks(trusted_proxy_networks)
    trusted = any(
        immediate.version == network.version and immediate in network for network in networks
    )
    if not trusted:
        return str(immediate)
    if type(headers) not in {tuple, list}:
        raise ClientIdentityError from None
    if len(cast(Sequence[object], headers)) > CONTROL_MAX_HEADERS:
        raise ClientIdentityError from None
    bounded_headers = (
        tuple(tuple.__iter__(cast(tuple[object, ...], headers)))
        if type(headers) is tuple
        else tuple(list.__iter__(cast(list[object], headers)))
    )
    forwarded: bytes | None = None
    try:
        for item in bounded_headers:
            if type(item) is not tuple or len(item) != 2:
                raise ClientIdentityError
            name = tuple.__getitem__(item, 0)
            value = tuple.__getitem__(item, 1)
            if type(name) is not bytes or type(value) is not bytes:
                raise ClientIdentityError
            if name.lower() == b"x-forwarded-for":
                if forwarded is not None:
                    raise ClientIdentityError
                forwarded = value
    except ClientIdentityError:
        raise
    except BaseException:
        raise ClientIdentityError from None
    if forwarded is None:
        return str(immediate)
    if len(forwarded) > CONTROL_MAX_XFF_BYTES:
        raise ClientIdentityError from None
    try:
        text = forwarded.decode("ascii")
    except UnicodeDecodeError:
        raise ClientIdentityError from None
    tokens = text.split(",")
    if not 1 <= len(tokens) <= CONTROL_MAX_XFF_HOPS:
        raise ClientIdentityError from None
    chain = [_canonical_ip(token.strip()) for token in tokens]
    chain.append(immediate)
    for address in reversed(chain):
        if not any(
            address.version == network.version and address in network for network in networks
        ):
            return str(address)
    return str(chain[0])


@_detached_configuration_boundary
def decode_digest_key(value: object) -> bytes:
    if type(value) is not str or len(value) != 43 or value != value.strip() or "=" in value:
        raise ControlConfigurationError from None
    if not value.isascii():
        raise ControlConfigurationError from None
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ControlConfigurationError from None
    if len(decoded) != 32:
        raise ControlConfigurationError from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != value:
        raise ControlConfigurationError from None
    return decoded


@_detached_configuration_boundary
def opaque_identity_digest(key: object, domain: object, identity: object) -> str:
    if type(key) is not bytes or len(key) != 32:
        raise ControlConfigurationError from None
    if type(domain) is not str or domain not in {"ip", "session"}:
        raise ControlConfigurationError from None
    if type(identity) is not str:
        raise ControlConfigurationError from None
    framed = _CONTROL_PREFIX + b"\x00" + domain.encode("ascii") + b"\x00" + identity.encode("utf-8")
    return hmac.new(key, framed, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class EndpointControlConfig(metaclass=_DetachedConfigMeta):
    trusted_proxy_cidrs: tuple[str | ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    digest_key: bytes
    ip_capacity: int
    ip_refill_per_minute: Decimal
    session_capacity: int
    session_refill_per_minute: Decimal
    rate_state_ttl_seconds: int
    max_keys: int
    max_active_requests: int
    max_queued_requests: int
    queue_wait_seconds: Decimal
    lease_ttl_seconds: Decimal
    lease_renew_seconds: Decimal
    budget_currency: str
    budget_hourly: Decimal
    budget_daily: Decimal
    budget_reserve_per_attempt: Decimal
    maximum_provider_attempts: int = 2

    def __repr__(self) -> str:
        return "EndpointControlConfig()"

    def __post_init__(self) -> None:
        networks = _networks(self.trusted_proxy_cidrs)
        if type(self.digest_key) is not bytes or len(self.digest_key) != 32:
            raise ControlConfigurationError from None
        ip_capacity = _checked_int(self.ip_capacity)
        session_capacity = _checked_int(self.session_capacity)
        active = _checked_int(self.max_active_requests)
        queued = _checked_int(self.max_queued_requests, minimum=0)
        ttl = _checked_int(self.rate_state_ttl_seconds)
        maximum_keys = _checked_int(self.max_keys)
        ip_refill = _strict_decimal(self.ip_refill_per_minute, positive=True)
        session_refill = _strict_decimal(self.session_refill_per_minute, positive=True)
        queue_wait = _strict_decimal(
            self.queue_wait_seconds,
            minimum=Decimal(0),
            maximum=Decimal(300),
            seconds=True,
        )
        lease_ttl = _strict_decimal(
            self.lease_ttl_seconds,
            minimum=Decimal(30),
            maximum=Decimal(600),
            seconds=True,
        )
        renew = _strict_decimal(
            self.lease_renew_seconds,
            minimum=Decimal(str(CONTROL_MIN_LEASE_RENEW_SECONDS)),
            seconds=True,
        )
        if _decimal_multiply(renew, Decimal(3)) > lease_ttl:
            raise ControlConfigurationError from None
        if (
            type(self.budget_currency) is not str
            or len(self.budget_currency) != 3
            or not self.budget_currency.isascii()
            or not self.budget_currency.isupper()
            or not self.budget_currency.isalpha()
        ):
            raise ControlConfigurationError from None
        hourly = _strict_decimal(self.budget_hourly, positive=True)
        daily = _strict_decimal(self.budget_daily, positive=True)
        reserve = _strict_decimal(self.budget_reserve_per_attempt, positive=True)
        maximum_attempts = _checked_int(self.maximum_provider_attempts, minimum=0, maximum=2)
        reserve_total = _decimal_multiply(reserve, Decimal(maximum_attempts))
        _strict_decimal(reserve_total, minimum=Decimal(0))
        if hourly > daily or reserve_total > hourly or reserve_total > daily:
            raise ControlConfigurationError from None
        with localcontext(_CONTROL_DECIMAL_CONTEXT):
            refill_ip_seconds = Decimal(ip_capacity) * Decimal(60) / ip_refill
            refill_session_seconds = Decimal(session_capacity) * Decimal(60) / session_refill
        if Decimal(ttl) < max(refill_ip_seconds, refill_session_seconds) or ttl > 31 * 24 * 3600:
            raise ControlConfigurationError from None
        poll_attempts = 0 if queue_wait == 0 else math.ceil(queue_wait)
        if any(value > CONTROL_MAX_INTEGER for value in (poll_attempts, active, queued)):
            raise ControlConfigurationError from None
        minimum_receipts = (
            1 + active * (CONTROL_MAX_RENEWAL_TICKS_PER_LEASE + 2) + queued * (poll_attempts + 2)
        )
        structural = 4 + 5 * active + 3 * queued
        minimum_keys = structural + minimum_receipts
        if (
            any(
                value > CONTROL_MAX_INTEGER
                for value in (minimum_receipts, structural, minimum_keys)
            )
            or maximum_keys < minimum_keys
        ):
            raise ControlConfigurationError from None
        object.__setattr__(self, "trusted_proxy_cidrs", networks)
        object.__setattr__(self, "digest_key", bytes(self.digest_key))

    @property
    def poll_attempts(self) -> int:
        return 0 if self.queue_wait_seconds == 0 else math.ceil(self.queue_wait_seconds)

    @property
    def minimum_receipts(self) -> int:
        return (
            1
            + self.max_active_requests * (CONTROL_MAX_RENEWAL_TICKS_PER_LEASE + 2)
            + self.max_queued_requests * (self.poll_attempts + 2)
        )

    @property
    def structural_reserve(self) -> int:
        return 4 + 5 * self.max_active_requests + 3 * self.max_queued_requests

    @property
    def minimum_keys(self) -> int:
        return self.structural_reserve + self.minimum_receipts

    @property
    def receipt_capacity(self) -> int:
        return self.max_keys - self.structural_reserve

    @property
    def ticket_ttl_seconds(self) -> Decimal:
        return _decimal_add(
            self.queue_wait_seconds,
            Decimal(2 * CONTROL_STORE_CALL_TIMEOUT_SECONDS),
        )


def _validated_config(value: object) -> EndpointControlConfig:
    if type(value) is not EndpointControlConfig:
        raise ControlConfigurationError from None
    failed = False
    try:
        trusted_proxy_cidrs = object.__getattribute__(value, "trusted_proxy_cidrs")
        digest_key = object.__getattribute__(value, "digest_key")
        if type(trusted_proxy_cidrs) is not tuple or type(digest_key) is not bytes:
            raise ControlConfigurationError from None
        result = EndpointControlConfig(
            trusted_proxy_cidrs=tuple(tuple.__iter__(trusted_proxy_cidrs)),
            digest_key=digest_key,
            ip_capacity=object.__getattribute__(value, "ip_capacity"),
            ip_refill_per_minute=object.__getattribute__(value, "ip_refill_per_minute"),
            session_capacity=object.__getattribute__(value, "session_capacity"),
            session_refill_per_minute=object.__getattribute__(value, "session_refill_per_minute"),
            rate_state_ttl_seconds=object.__getattribute__(value, "rate_state_ttl_seconds"),
            max_keys=object.__getattribute__(value, "max_keys"),
            max_active_requests=object.__getattribute__(value, "max_active_requests"),
            max_queued_requests=object.__getattribute__(value, "max_queued_requests"),
            queue_wait_seconds=object.__getattribute__(value, "queue_wait_seconds"),
            lease_ttl_seconds=object.__getattribute__(value, "lease_ttl_seconds"),
            lease_renew_seconds=object.__getattribute__(value, "lease_renew_seconds"),
            budget_currency=object.__getattribute__(value, "budget_currency"),
            budget_hourly=object.__getattribute__(value, "budget_hourly"),
            budget_daily=object.__getattribute__(value, "budget_daily"),
            budget_reserve_per_attempt=object.__getattribute__(value, "budget_reserve_per_attempt"),
            maximum_provider_attempts=object.__getattribute__(value, "maximum_provider_attempts"),
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
        raise
    except BaseException as error:
        _release_exception(error)
        failed = True
    if failed:
        del value
        raise ControlConfigurationError from None
    return result


def _decimal_setting(value: object) -> Decimal:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 39
        or _CANONICAL_DECIMAL.fullmatch(value) is None
    ):
        raise ControlConfigurationError from None
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ControlConfigurationError from None
    if format(result, "f") != value and not (value == "0" and result == 0):
        raise ControlConfigurationError from None
    return result


def _endpoint_control_config_from_settings_inner(
    settings: object,
) -> EndpointControlConfig | None:
    """Validate the cross-field policy after the source-free settings snapshot."""

    try:
        enabled = object.__getattribute__(settings, "public_endpoint_controls_enabled")
        deployment_mode = object.__getattribute__(settings, "deployment_mode")
    except BaseException:
        raise ControlConfigurationError from None
    if type(enabled) is not bool or type(deployment_mode) is not str:
        raise ControlConfigurationError from None
    if not enabled:
        if deployment_mode == "production":
            raise ControlConfigurationError from None
        return None
    names = (
        "trusted_proxy_cidrs",
        "public_control_digest_key",
        "public_rate_ip_capacity",
        "public_rate_ip_refill_per_minute",
        "public_rate_session_capacity",
        "public_rate_session_refill_per_minute",
        "public_rate_state_ttl_seconds",
        "public_control_max_keys",
        "public_max_active_requests",
        "public_max_queued_requests",
        "public_queue_wait_seconds",
        "public_lease_ttl_seconds",
        "public_lease_renew_seconds",
        "public_budget_currency",
        "public_budget_hourly",
        "public_budget_daily",
        "public_budget_reserve_per_attempt",
        "provider",
        "gemini_max_retries",
    )
    try:
        raw = {name: object.__getattribute__(settings, name) for name in names}
    except BaseException:
        raise ControlConfigurationError from None
    cidr_text = raw["trusted_proxy_cidrs"]
    if (
        type(cidr_text) is not str
        or len(cidr_text) > CONTROL_MAX_CIDR_TEXT_BYTES
        or cidr_text.count(",") >= CONTROL_MAX_TRUSTED_CIDRS
    ):
        raise ControlConfigurationError from None
    cidrs: tuple[str, ...]
    if cidr_text == "":
        cidrs = ()
    else:
        parts = cidr_text.split(",")
        if any(not item or item != item.strip() for item in parts):
            raise ControlConfigurationError from None
        cidrs = tuple(parts)
    secret = raw["public_control_digest_key"]
    if type(secret) is not SecretStr:
        raise ControlConfigurationError from None
    try:
        secret_value = object.__getattribute__(secret, "_secret_value")
    except BaseException:
        raise ControlConfigurationError from None
    digest_key = decode_digest_key(secret_value)
    provider = raw["provider"]
    retries = raw["gemini_max_retries"]
    if type(provider) is not str or type(retries) is not int:
        raise ControlConfigurationError from None
    maximum_attempts = (
        0
        if provider == "echo"
        else 1
        if provider == "ollama"
        else retries + 1
        if provider == "gemini"
        else -1
    )
    config = EndpointControlConfig(
        trusted_proxy_cidrs=cidrs,
        digest_key=digest_key,
        ip_capacity=_checked_int(raw["public_rate_ip_capacity"]),
        ip_refill_per_minute=_decimal_setting(raw["public_rate_ip_refill_per_minute"]),
        session_capacity=_checked_int(raw["public_rate_session_capacity"]),
        session_refill_per_minute=_decimal_setting(raw["public_rate_session_refill_per_minute"]),
        rate_state_ttl_seconds=_checked_int(raw["public_rate_state_ttl_seconds"]),
        max_keys=_checked_int(raw["public_control_max_keys"]),
        max_active_requests=_checked_int(raw["public_max_active_requests"]),
        max_queued_requests=_checked_int(raw["public_max_queued_requests"], minimum=0),
        queue_wait_seconds=_decimal_setting(raw["public_queue_wait_seconds"]),
        lease_ttl_seconds=_decimal_setting(raw["public_lease_ttl_seconds"]),
        lease_renew_seconds=_decimal_setting(raw["public_lease_renew_seconds"]),
        budget_currency=cast(str, raw["public_budget_currency"]),
        budget_hourly=_decimal_setting(raw["public_budget_hourly"]),
        budget_daily=_decimal_setting(raw["public_budget_daily"]),
        budget_reserve_per_attempt=_decimal_setting(raw["public_budget_reserve_per_attempt"]),
        maximum_provider_attempts=maximum_attempts,
    )
    parsed_networks = _networks(config.trusted_proxy_cidrs)
    if deployment_mode == "production" and any(
        network.prefixlen == 0 for network in parsed_networks
    ):
        raise ControlConfigurationError from None
    return config


def endpoint_control_config_from_settings(settings: object) -> EndpointControlConfig | None:
    failed = False
    try:
        result = _endpoint_control_config_from_settings_inner(settings)
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
        raise
    except BaseException as error:
        _release_exception(error)
        failed = True
    if failed:
        del settings
        raise ControlConfigurationError from None
    return result


@dataclass(frozen=True, slots=True)
class AdmissionRequest(metaclass=_DetachedStoreMeta):
    ip_digest: str
    session_digest: str
    max_provider_attempts: int

    def __post_init__(self) -> None:
        if (
            not _is_hex(self.ip_digest, _HEX_64_LENGTH)
            or not _is_hex(self.session_digest, _HEX_64_LENGTH)
            or type(self.max_provider_attempts) is not int
            or not 0 <= self.max_provider_attempts <= 2
        ):
            raise ControlStoreError("malformed_store") from None


def _copy_admission_request(value: object) -> AdmissionRequest:
    if type(value) is not AdmissionRequest:
        raise ControlStoreError("malformed_store") from None
    try:
        return AdmissionRequest(
            ip_digest=object.__getattribute__(value, "ip_digest"),
            session_digest=object.__getattribute__(value, "session_digest"),
            max_provider_attempts=object.__getattribute__(value, "max_provider_attempts"),
        )
    except ControlStoreError:
        raise
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
        raise
    except Exception:
        raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class BudgetReconciliation(metaclass=_DetachedStoreMeta):
    charge: Decimal
    currency: str
    accounting_uncertain: bool

    def __post_init__(self) -> None:
        try:
            charge = _strict_decimal(self.charge, minimum=Decimal(0))
        except ControlConfigurationError:
            raise ControlStoreError("malformed_store") from None
        if (
            charge != self.charge
            or type(self.currency) is not str
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
            or not self.currency.isalpha()
            or type(self.accounting_uncertain) is not bool
        ):
            raise ControlStoreError("malformed_store") from None


def _copy_reconciliation(value: object) -> BudgetReconciliation:
    if type(value) is not BudgetReconciliation:
        raise ControlStoreError("malformed_store") from None
    try:
        return BudgetReconciliation(
            charge=object.__getattribute__(value, "charge"),
            currency=object.__getattribute__(value, "currency"),
            accounting_uncertain=object.__getattribute__(value, "accounting_uncertain"),
        )
    except ControlStoreError:
        raise
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
        raise
    except Exception:
        raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class ControlTicket(metaclass=_DetachedStoreMeta):
    ticket_id: str
    fencing_token: str
    expires_at: str

    def __post_init__(self) -> None:
        if not _is_hex(self.ticket_id, 32) or not _is_hex(self.fencing_token, 32):
            raise ControlStoreError("malformed_store") from None
        parse_canonical_utc_instant(self.expires_at)


@dataclass(frozen=True, slots=True)
class ControlLease(metaclass=_DetachedStoreMeta):
    lease_id: str
    fencing_token: str
    expires_at: str
    reservation_instant: str
    attempt_date: date
    hour_window: str
    day_window: str
    reserved_amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not _is_hex(self.lease_id, 32) or not _is_hex(self.fencing_token, 32):
            raise ControlStoreError("malformed_store") from None
        if (
            type(self.expires_at) is not str
            or type(self.reservation_instant) is not str
            or type(self.attempt_date) is not date
            or type(self.hour_window) is not str
            or type(self.day_window) is not str
            or type(self.currency) is not str
        ):
            raise ControlStoreError("malformed_store") from None
        instant = parse_canonical_utc_instant(self.reservation_instant)
        expires = parse_canonical_utc_instant(self.expires_at)
        if (
            instant > _MAX_BASE_INSTANT
            or self.attempt_date != instant.date()
            or self.hour_window != _hour_identity(instant)
            or self.day_window != _day_identity(instant)
        ):
            raise ControlStoreError("malformed_store") from None
        try:
            _strict_decimal(self.reserved_amount, minimum=Decimal(0))
        except ControlConfigurationError:
            raise ControlStoreError("malformed_store") from None
        if (
            expires <= instant
            or type(self.currency) is not str
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
            or not self.currency.isalpha()
        ):
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class Admitted(metaclass=_DetachedStoreMeta):
    lease: ControlLease
    outcome: Literal["admitted"] = "admitted"

    def __post_init__(self) -> None:
        if (
            type(self.lease) is not ControlLease
            or type(self.outcome) is not str
            or self.outcome != "admitted"
        ):
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class Queued(metaclass=_DetachedStoreMeta):
    ticket: ControlTicket
    outcome: Literal["queued"] = "queued"

    def __post_init__(self) -> None:
        if (
            type(self.ticket) is not ControlTicket
            or type(self.outcome) is not str
            or self.outcome != "queued"
        ):
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class Denied(metaclass=_DetachedStoreMeta):
    reason: DenialReason
    outcome: Literal["denied"] = "denied"

    def __post_init__(self) -> None:
        if (
            type(self.reason) is not str
            or self.reason
            not in {
                "ip_rate_limited",
                "session_rate_limited",
                "budget_exhausted",
                "concurrency_limited",
            }
            or type(self.outcome) is not str
            or self.outcome != "denied"
        ):
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class Pending(metaclass=_DetachedStoreMeta):
    ticket: ControlTicket
    outcome: Literal["pending"] = "pending"

    def __post_init__(self) -> None:
        if (
            type(self.ticket) is not ControlTicket
            or type(self.outcome) is not str
            or self.outcome != "pending"
        ):
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class Renewed(metaclass=_DetachedStoreMeta):
    expires_at: str
    outcome: Literal["renewed"] = "renewed"

    def __post_init__(self) -> None:
        parse_canonical_utc_instant(self.expires_at)
        if type(self.outcome) is not str or self.outcome != "renewed":
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class Lost(metaclass=_DetachedStoreMeta):
    reason: Literal["fencing_lost", "lease_expired"]
    outcome: Literal["lost"] = "lost"

    def __post_init__(self) -> None:
        if (
            type(self.reason) is not str
            or self.reason not in {"fencing_lost", "lease_expired"}
            or type(self.outcome) is not str
            or self.outcome != "lost"
        ):
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class Finalized(metaclass=_DetachedStoreMeta):
    outcome: Literal["finalized"] = "finalized"

    def __post_init__(self) -> None:
        if type(self.outcome) is not str or self.outcome != "finalized":
            raise ControlStoreError("malformed_store") from None


@dataclass(frozen=True, slots=True)
class CancelledTicket(metaclass=_DetachedStoreMeta):
    outcome: Literal["cancelled"] = "cancelled"

    def __post_init__(self) -> None:
        if type(self.outcome) is not str or self.outcome != "cancelled":
            raise ControlStoreError("malformed_store") from None


AdmissionOutcome = Admitted | Queued | Denied
PollOutcome = Pending | Admitted | Denied
RenewOutcome = Renewed | Lost
MutationOutcome = AdmissionOutcome | PollOutcome | RenewOutcome | Finalized | CancelledTicket


@dataclass(frozen=True, slots=True)
class ControlOperationReceipt(metaclass=_DetachedStoreMeta):
    operation_id: str
    kind: OperationKind
    request_fingerprint: str
    outcome: MutationOutcome
    committed_at: str
    expires_at: str
    schema_version: Literal["1.0"] = "1.0"

    def __post_init__(self) -> None:
        if (
            not _is_hex(self.operation_id, 32)
            or type(self.kind) is not str
            or self.kind
            not in {
                "admit_or_enqueue",
                "wait_for_admission",
                "renew",
                "finalize",
                "cancel_ticket",
            }
            or not _is_hex(self.request_fingerprint, 64)
            or type(self.committed_at) is not str
            or type(self.expires_at) is not str
            or type(self.schema_version) is not str
            or self.schema_version != CONTROL_RECEIPT_SCHEMA_VERSION
        ):
            raise ControlStoreError("malformed_store") from None
        committed = parse_canonical_utc_instant(self.committed_at)
        expires = parse_canonical_utc_instant(self.expires_at)
        copied = _copy_outcome(self.kind, self.outcome)
        outcome_time_valid = True
        if type(copied) is Admitted:
            outcome_time_valid = copied.lease.reservation_instant == self.committed_at
        elif type(copied) is Queued:
            outcome_time_valid = parse_canonical_utc_instant(copied.ticket.expires_at) > committed
        elif type(copied) is Pending:
            outcome_time_valid = parse_canonical_utc_instant(copied.ticket.expires_at) > committed
        elif type(copied) is Renewed:
            outcome_time_valid = parse_canonical_utc_instant(copied.expires_at) > committed
        if committed > _MAX_BASE_INSTANT or expires <= committed or not outcome_time_valid:
            raise ControlStoreError("malformed_store") from None
        object.__setattr__(self, "outcome", copied)


def _outcome_matches(kind: OperationKind, outcome: MutationOutcome) -> bool:
    if kind == "admit_or_enqueue":
        return type(outcome) in {Admitted, Queued, Denied}
    if kind == "wait_for_admission":
        return type(outcome) in {Pending, Admitted, Denied} and not (
            type(outcome) is Denied
            and outcome.reason in {"ip_rate_limited", "session_rate_limited"}
        )
    if kind == "renew":
        return type(outcome) in {Renewed, Lost}
    if kind == "finalize":
        return type(outcome) is Finalized
    return kind == "cancel_ticket" and type(outcome) is CancelledTicket


def _copy_ticket(value: object) -> ControlTicket:
    if type(value) is not ControlTicket:
        raise ControlStoreError("malformed_store") from None
    try:
        return ControlTicket(
            ticket_id=object.__getattribute__(value, "ticket_id"),
            fencing_token=object.__getattribute__(value, "fencing_token"),
            expires_at=object.__getattribute__(value, "expires_at"),
        )
    except ControlStoreError:
        raise
    except BaseException:
        raise ControlStoreError("malformed_store") from None


def _copy_lease(value: object) -> ControlLease:
    if type(value) is not ControlLease:
        raise ControlStoreError("malformed_store") from None
    try:
        return ControlLease(
            lease_id=object.__getattribute__(value, "lease_id"),
            fencing_token=object.__getattribute__(value, "fencing_token"),
            expires_at=object.__getattribute__(value, "expires_at"),
            reservation_instant=object.__getattribute__(value, "reservation_instant"),
            attempt_date=object.__getattribute__(value, "attempt_date"),
            hour_window=object.__getattribute__(value, "hour_window"),
            day_window=object.__getattribute__(value, "day_window"),
            reserved_amount=object.__getattribute__(value, "reserved_amount"),
            currency=object.__getattribute__(value, "currency"),
        )
    except ControlStoreError:
        raise
    except BaseException:
        raise ControlStoreError("malformed_store") from None


def _require_outcome_discriminator(value: object, expected: str) -> None:
    raw = object.__getattribute__(value, "outcome")
    if type(raw) is not str or raw != expected:
        raise ControlStoreError("malformed_store") from None


def _copy_outcome(kind: OperationKind, value: object) -> MutationOutcome:
    try:
        if type(value) is Admitted:
            _require_outcome_discriminator(value, "admitted")
            copied: MutationOutcome = Admitted(_copy_lease(object.__getattribute__(value, "lease")))
        elif type(value) is Queued:
            _require_outcome_discriminator(value, "queued")
            copied = Queued(_copy_ticket(object.__getattribute__(value, "ticket")))
        elif type(value) is Pending:
            _require_outcome_discriminator(value, "pending")
            copied = Pending(_copy_ticket(object.__getattribute__(value, "ticket")))
        elif type(value) is Denied:
            _require_outcome_discriminator(value, "denied")
            copied = Denied(object.__getattribute__(value, "reason"))
        elif type(value) is Renewed:
            _require_outcome_discriminator(value, "renewed")
            copied = Renewed(object.__getattribute__(value, "expires_at"))
        elif type(value) is Lost:
            _require_outcome_discriminator(value, "lost")
            copied = Lost(object.__getattribute__(value, "reason"))
        elif type(value) is Finalized:
            _require_outcome_discriminator(value, "finalized")
            copied = Finalized()
        elif type(value) is CancelledTicket:
            _require_outcome_discriminator(value, "cancelled")
            copied = CancelledTicket()
        else:
            raise ControlStoreError("malformed_store")
    except ControlStoreError:
        raise
    except BaseException:
        raise ControlStoreError("malformed_store") from None
    if not _outcome_matches(kind, copied):
        raise ControlStoreError("malformed_store") from None
    return copied


def _normalized_outcome(kind: OperationKind, value: object) -> MutationOutcome:
    if type(value) is types.CoroutineType:
        value.close()
        raise ControlStoreError("malformed_store") from None
    failed = False
    try:
        copied = _copy_outcome(kind, value)
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
        raise
    except Exception as error:
        _release_exception(error)
        failed = True
    if failed:
        del value
        raise ControlStoreError("malformed_store") from None
    return copied


def _detached_receipt_adapter[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    @wraps(operation)
    def detached(*args: P.args, **kwargs: P.kwargs) -> R:
        failed = False
        primary: BaseException | None = None
        try:
            result = operation(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as error:
            primary = error
        except BaseException as error:
            _release_exception(error)
            failed = True
        if primary is not None:
            exact_primary = primary
            del args, kwargs, primary
            raise exact_primary.with_traceback(None) from None
        if failed:
            del args, kwargs
            raise ControlStoreError("malformed_store") from None
        return result

    return detached


@_detached_receipt_adapter
def copy_control_operation_receipt(value: object) -> ControlOperationReceipt:
    if type(value) is not ControlOperationReceipt:
        raise ControlStoreError("malformed_store") from None
    try:
        return ControlOperationReceipt(
            operation_id=object.__getattribute__(value, "operation_id"),
            kind=object.__getattribute__(value, "kind"),
            request_fingerprint=object.__getattribute__(value, "request_fingerprint"),
            outcome=object.__getattribute__(value, "outcome"),
            committed_at=object.__getattribute__(value, "committed_at"),
            expires_at=object.__getattribute__(value, "expires_at"),
            schema_version=object.__getattribute__(value, "schema_version"),
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
        raise
    except Exception:
        raise ControlStoreError("malformed_store") from None


def _ticket_payload(ticket: ControlTicket) -> dict[str, object]:
    copied = _copy_ticket(ticket)
    return {
        "expires_at": copied.expires_at,
        "fencing_token": copied.fencing_token,
        "ticket_id": copied.ticket_id,
    }


def _lease_payload(lease: ControlLease) -> dict[str, object]:
    copied = _copy_lease(lease)
    return {
        "attempt_date": copied.attempt_date.isoformat(),
        "currency": copied.currency,
        "day_window": copied.day_window,
        "expires_at": copied.expires_at,
        "fencing_token": copied.fencing_token,
        "hour_window": copied.hour_window,
        "lease_id": copied.lease_id,
        "reservation_instant": copied.reservation_instant,
        "reserved_amount": format(copied.reserved_amount, "f"),
    }


def _outcome_payload(outcome: MutationOutcome) -> dict[str, object]:
    if type(outcome) is Admitted:
        return {"lease": _lease_payload(outcome.lease), "outcome": "admitted"}
    if type(outcome) is Queued:
        return {"outcome": "queued", "ticket": _ticket_payload(outcome.ticket)}
    if type(outcome) is Pending:
        return {"outcome": "pending", "ticket": _ticket_payload(outcome.ticket)}
    if type(outcome) is Denied:
        return {"outcome": "denied", "reason": outcome.reason}
    if type(outcome) is Renewed:
        return {"expires_at": outcome.expires_at, "outcome": "renewed"}
    if type(outcome) is Lost:
        return {"outcome": "lost", "reason": outcome.reason}
    if type(outcome) is Finalized:
        return {"outcome": "finalized"}
    if type(outcome) is CancelledTicket:
        return {"outcome": "cancelled"}
    raise ControlStoreError("malformed_store") from None


@_detached_receipt_adapter
def control_operation_receipt_to_json(value: object) -> str:
    receipt = copy_control_operation_receipt(value)
    payload = {
        "committed_at": receipt.committed_at,
        "expires_at": receipt.expires_at,
        "kind": receipt.kind,
        "operation_id": receipt.operation_id,
        "outcome": _outcome_payload(receipt.outcome),
        "request_fingerprint": receipt.request_fingerprint,
        "schema_version": receipt.schema_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded) > CONTROL_MAX_RECEIPT_JSON_BYTES:
        raise ControlStoreError("malformed_store") from None
    return encoded


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise ControlStoreError("malformed_store") from None
    return cast(dict[str, object], value)


def _parse_ticket_payload(value: object) -> ControlTicket:
    payload = _exact_mapping(value, frozenset(("expires_at", "fencing_token", "ticket_id")))
    return ControlTicket(**cast(Any, payload))


def _parse_lease_payload(value: object) -> ControlLease:
    payload = _exact_mapping(
        value,
        frozenset(
            (
                "attempt_date",
                "currency",
                "day_window",
                "expires_at",
                "fencing_token",
                "hour_window",
                "lease_id",
                "reservation_instant",
                "reserved_amount",
            )
        ),
    )
    raw_date = payload["attempt_date"]
    raw_amount = payload["reserved_amount"]
    if (
        type(raw_date) is not str
        or len(raw_date) != 10
        or not raw_date.isascii()
        or raw_date[4] != "-"
        or raw_date[7] != "-"
        or not (raw_date[:4] + raw_date[5:7] + raw_date[8:]).isdecimal()
        or type(raw_amount) is not str
        or len(raw_amount) > 64
        or _CANONICAL_DECIMAL.fullmatch(raw_amount) is None
    ):
        raise ControlStoreError("malformed_store") from None
    try:
        attempt_date = date.fromisoformat(raw_date)
        amount = Decimal(raw_amount)
    except (ValueError, InvalidOperation):
        raise ControlStoreError("malformed_store") from None
    if attempt_date.isoformat() != raw_date:
        raise ControlStoreError("malformed_store") from None
    normalized = dict(payload)
    normalized["attempt_date"] = attempt_date
    normalized["reserved_amount"] = amount
    return ControlLease(**cast(Any, normalized))


def _parse_outcome_payload(value: object) -> MutationOutcome:
    if type(value) is not dict:
        raise ControlStoreError("malformed_store") from None
    payload = cast(dict[str, object], value)
    discriminator = payload.get("outcome")
    if type(discriminator) is not str:
        raise ControlStoreError("malformed_store") from None
    if discriminator == "admitted":
        exact = _exact_mapping(payload, frozenset(("lease", "outcome")))
        return Admitted(_parse_lease_payload(exact["lease"]))
    if discriminator in {"queued", "pending"}:
        exact = _exact_mapping(payload, frozenset(("outcome", "ticket")))
        ticket = _parse_ticket_payload(exact["ticket"])
        return Queued(ticket) if discriminator == "queued" else Pending(ticket)
    if discriminator == "denied":
        exact = _exact_mapping(payload, frozenset(("outcome", "reason")))
        return Denied(cast(Any, exact["reason"]))
    if discriminator == "renewed":
        exact = _exact_mapping(payload, frozenset(("expires_at", "outcome")))
        return Renewed(cast(Any, exact["expires_at"]))
    if discriminator == "lost":
        exact = _exact_mapping(payload, frozenset(("outcome", "reason")))
        return Lost(cast(Any, exact["reason"]))
    if discriminator == "finalized":
        _exact_mapping(payload, frozenset(("outcome",)))
        return Finalized()
    if discriminator == "cancelled":
        _exact_mapping(payload, frozenset(("outcome",)))
        return CancelledTicket()
    raise ControlStoreError("malformed_store") from None


@_detached_receipt_adapter
def control_operation_receipt_from_json(value: object) -> ControlOperationReceipt:
    if type(value) is not str or len(value) > CONTROL_MAX_RECEIPT_JSON_BYTES or not value.isascii():
        raise ControlStoreError("malformed_store") from None

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        decoded = json.loads(value, object_pairs_hook=pairs_hook)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ControlStoreError("malformed_store") from None
    payload = _exact_mapping(
        decoded,
        frozenset(
            (
                "committed_at",
                "expires_at",
                "kind",
                "operation_id",
                "outcome",
                "request_fingerprint",
                "schema_version",
            )
        ),
    )
    normalized = dict(payload)
    normalized["outcome"] = _parse_outcome_payload(payload["outcome"])
    return ControlOperationReceipt(**cast(Any, normalized))


@runtime_checkable
class EndpointControlStore(Protocol):
    async def admit_or_enqueue(
        self, operation_id: str, request: AdmissionRequest
    ) -> AdmissionOutcome: ...
    async def wait_for_admission(
        self, operation_id: str, ticket_id: str, fencing_token: str
    ) -> PollOutcome: ...
    async def renew(self, operation_id: str, lease_id: str, fencing_token: str) -> RenewOutcome: ...
    async def finalize(
        self,
        operation_id: str,
        lease_id: str,
        fencing_token: str,
        reconciliation: BudgetReconciliation,
    ) -> Finalized: ...
    async def cancel_ticket(
        self, operation_id: str, ticket_id: str, fencing_token: str
    ) -> CancelledTicket: ...
    async def check_budget_readiness(self) -> object: ...


class ProductionEndpointControlStore:
    """Nominal marker required of an authoritative shared production adapter."""

    __slots__ = ()


@dataclass(slots=True)
class _RateRecord:
    tokens: Decimal
    updated_at: datetime


@dataclass(slots=True)
class _TicketState:
    ticket: ControlTicket
    request: AdmissionRequest
    sequence: int
    polls_remaining: int


@dataclass(slots=True)
class _LeaseState:
    lease: ControlLease
    renewals_remaining: int = CONTROL_MAX_RENEWAL_TICKS_PER_LEASE


@dataclass(slots=True)
class _BudgetWindow:
    reserved: Decimal
    committed: Decimal
    expires_at: datetime
    uncertain: bool = False
    overrun: bool = False


class InMemoryEndpointControlStore(metaclass=_DetachedConfigMeta):
    """Atomic deterministic conformance store; never selected for production."""

    def __init__(
        self,
        config: EndpointControlConfig,
        *,
        clock: Callable[[], datetime],
        token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        if (
            type(config) is not EndpointControlConfig
            or not callable(clock)
            or not callable(token_factory)
        ):
            raise ControlConfigurationError from None
        self._config = _validated_config(config)
        self._clock = clock
        self._token_factory = token_factory
        self._lock = asyncio.Lock()
        self._last_now: datetime | None = None
        self._ip_rates: dict[str, _RateRecord] = {}
        self._session_rates: dict[str, _RateRecord] = {}
        self._tickets: dict[str, _TicketState] = {}
        self._leases: dict[str, _LeaseState] = {}
        self._hours: dict[str, _BudgetWindow] = {}
        self._days: dict[str, _BudgetWindow] = {}
        self._receipts: dict[str, ControlOperationReceipt] = {}
        self._sequence = 0
        self._token_sequence = 0

    def _now(self) -> datetime:
        failed = False
        try:
            raw_now: object = self._clock()
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                raise
            _release_exception(error)
            failed = True
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as error:
            _release_exception(error)
            failed = True
        if failed:
            del self
            raise ControlStoreError("store_unavailable") from None
        if type(raw_now) is types.CoroutineType:
            raw_now.close()
            raise ControlStoreError("malformed_store") from None
        if type(raw_now) is not datetime:
            raise ControlStoreError("malformed_store") from None
        now = raw_now
        canonical_utc_instant(now)
        if now > _MAX_BASE_INSTANT:
            raise ControlStoreError("malformed_store") from None
        if self._last_now is not None and now < self._last_now:
            raise ControlStoreError("store_conflict") from None
        self._last_now = now
        return now

    def _token_in_use(self, token: str) -> bool:
        if any(
            token in {state.ticket.ticket_id, state.ticket.fencing_token}
            for state in self._tickets.values()
        ) or any(
            token in {state.lease.lease_id, state.lease.fencing_token}
            for state in self._leases.values()
        ):
            return True
        for receipt in self._receipts.values():
            outcome = _copy_outcome(receipt.kind, receipt.outcome)
            if type(outcome) is Queued:
                if token in {outcome.ticket.ticket_id, outcome.ticket.fencing_token}:
                    return True
            if type(outcome) is Pending:
                if token in {outcome.ticket.ticket_id, outcome.ticket.fencing_token}:
                    return True
            if type(outcome) is Admitted and token in {
                outcome.lease.lease_id,
                outcome.lease.fencing_token,
            }:
                return True
        return False

    def _raw_token(self) -> str:
        failed = False
        try:
            raw_token: object = self._token_factory()
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                raise
            _release_exception(error)
            failed = True
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as error:
            _release_exception(error)
            failed = True
        if failed:
            raise ControlStoreError("store_unavailable") from None
        if type(raw_token) is types.CoroutineType:
            raw_token.close()
            raise ControlStoreError("malformed_store") from None
        if not _is_hex(raw_token, 32):
            raise ControlStoreError("malformed_store") from None
        return cast(str, raw_token)

    def _plan_token_pair(self) -> tuple[str, str, int]:
        if self._token_sequence > CONTROL_MAX_INTEGER - 2:
            raise ControlStoreError("store_capacity") from None
        first_raw = self._raw_token()
        second_raw = self._raw_token()
        first_sequence = self._token_sequence + 1
        second_sequence = self._token_sequence + 2
        first = hashlib.sha256(
            bytes.fromhex(first_raw) + first_sequence.to_bytes(8, "big")
        ).hexdigest()[:32]
        second = hashlib.sha256(
            bytes.fromhex(second_raw) + second_sequence.to_bytes(8, "big")
        ).hexdigest()[:32]
        if first == second or self._token_in_use(first) or self._token_in_use(second):
            raise ControlStoreError("store_conflict") from None
        return first, second, second_sequence

    def _window_expiries(self, now: datetime) -> tuple[datetime, datetime]:
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return hour_end, day_end

    def _prune(self, now: datetime) -> None:
        for ticket_id, ticket_state in tuple(self._tickets.items()):
            if parse_canonical_utc_instant(ticket_state.ticket.expires_at) <= now:
                del self._tickets[ticket_id]
        for lease_id, lease_state in tuple(self._leases.items()):
            if parse_canonical_utc_instant(lease_state.lease.expires_at) <= now:
                lease = lease_state.lease
                self._release_reservation(lease, lease.reserved_amount, uncertain=True)
                del self._leases[lease_id]
        live_hours = {state.lease.hour_window for state in self._leases.values()}
        live_days = {state.lease.day_window for state in self._leases.values()}
        for key, window in tuple(self._hours.items()):
            if window.expires_at <= now and key not in live_hours:
                del self._hours[key]
        for key, window in tuple(self._days.items()):
            if window.expires_at <= now and key not in live_days:
                del self._days[key]
        for operation_id, receipt in tuple(self._receipts.items()):
            if parse_canonical_utc_instant(receipt.expires_at) <= now:
                outcome = receipt.outcome
                ticket_live = type(outcome) is Queued and outcome.ticket.ticket_id in self._tickets
                ticket_live = ticket_live or (
                    type(outcome) is Pending and outcome.ticket.ticket_id in self._tickets
                )
                lease_live = type(outcome) is Admitted and outcome.lease.lease_id in self._leases
                if not ticket_live and not lease_live:
                    del self._receipts[operation_id]

    def _refilled(
        self, record: _RateRecord, capacity: int, per_minute: Decimal, now: datetime
    ) -> Decimal:
        delta = now - record.updated_at
        with localcontext(_CONTROL_DECIMAL_CONTEXT):
            elapsed = Decimal(delta.days * 86_400 + delta.seconds) + Decimal(
                delta.microseconds
            ) / Decimal(1_000_000)
        if elapsed < 0:
            raise ControlStoreError("store_conflict") from None
        with localcontext(_CONTROL_DECIMAL_CONTEXT):
            refilled = record.tokens + elapsed * per_minute / Decimal(60)
            bounded = min(Decimal(capacity), refilled)
        try:
            return _strict_decimal(bounded, minimum=Decimal(0))
        except ControlConfigurationError:
            raise ControlStoreError("malformed_store") from None

    def _prune_rates(self, now: datetime) -> None:
        ttl = timedelta(seconds=self._config.rate_state_ttl_seconds)
        for mapping, capacity, refill in (
            (self._ip_rates, self._config.ip_capacity, self._config.ip_refill_per_minute),
            (
                self._session_rates,
                self._config.session_capacity,
                self._config.session_refill_per_minute,
            ),
        ):
            for key, record in tuple(mapping.items()):
                tokens = self._refilled(record, capacity, refill, now)
                if tokens == Decimal(capacity) and record.updated_at + ttl <= now:
                    del mapping[key]

    def _count(self) -> int:
        return (
            len(self._ip_rates)
            + len(self._session_rates)
            + len(self._tickets)
            + len(self._leases)
            + len(self._hours)
            + len(self._days)
            + len(self._receipts)
        )

    def _ensure_capacity(
        self,
        *,
        rates: int = 0,
        tickets: int = 0,
        leases: int = 0,
        budgets: int = 0,
        receipts: int = 1,
        future_receipt_delta: int = 0,
    ) -> None:
        rate_cap = 2 * (self._config.max_active_requests + self._config.max_queued_requests + 1)
        budget_cap = 2 * (self._config.max_active_requests + 1)
        if (
            len(self._receipts) + receipts + self._future_receipt_reserve() + future_receipt_delta
            > self._config.receipt_capacity
            or len(self._ip_rates) + len(self._session_rates) + rates > rate_cap
            or len(self._tickets) + tickets > self._config.max_queued_requests
            or len(self._leases) + leases > self._config.max_active_requests
            or len(self._hours) + len(self._days) + budgets > budget_cap
            or self._count() + rates + tickets + leases + budgets + receipts > self._config.max_keys
        ):
            raise ControlStoreError("store_capacity") from None

    def _future_receipt_reserve(self) -> int:
        return sum(state.renewals_remaining + 1 for state in self._leases.values()) + sum(
            state.polls_remaining + 1 for state in self._tickets.values()
        )

    @staticmethod
    def _fingerprint(payload: dict[str, object]) -> str:
        try:
            raw = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            ).encode("ascii")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ControlStoreError("malformed_store") from None
        return hashlib.sha256(raw).hexdigest()

    def _replay(
        self, operation_id: str, kind: OperationKind, fingerprint: str
    ) -> MutationOutcome | None:
        if not _is_hex(operation_id, 32):
            raise ControlStoreError("malformed_store") from None
        receipt = self._receipts.get(operation_id)
        if receipt is None:
            return None
        if receipt.kind != kind or receipt.request_fingerprint != fingerprint:
            raise ControlStoreError("malformed_store") from None
        return _copy_outcome(kind, receipt.outcome)

    def _preflight_receipt_collision(
        self, operation_id: str, kind: OperationKind, fingerprint: str
    ) -> None:
        receipt = self._receipts.get(operation_id)
        if receipt is not None and (
            receipt.kind != kind or receipt.request_fingerprint != fingerprint
        ):
            raise ControlStoreError("malformed_store") from None

    def _build_receipt(
        self,
        operation_id: str,
        kind: OperationKind,
        fingerprint: str,
        outcome: MutationOutcome,
        now: datetime,
    ) -> ControlOperationReceipt:
        seconds = max(
            Decimal(600),
            Decimal(self._config.rate_state_ttl_seconds),
            self._config.ticket_ttl_seconds,
            self._config.lease_ttl_seconds,
        )
        try:
            expiry = now + _decimal_seconds(seconds)
        except (OverflowError, ValueError):
            raise ControlStoreError("malformed_store") from None
        canonical_utc_instant(expiry)
        copied_outcome = _copy_outcome(kind, outcome)
        return ControlOperationReceipt(
            operation_id=operation_id,
            kind=kind,
            request_fingerprint=fingerprint,
            outcome=copied_outcome,
            committed_at=canonical_utc_instant(now),
            expires_at=canonical_utc_instant(expiry),
        )

    def _commit_receipt(self, receipt: ControlOperationReceipt) -> MutationOutcome:
        self._receipts[receipt.operation_id] = receipt
        return _copy_outcome(receipt.kind, receipt.outcome)

    def _record(
        self,
        operation_id: str,
        kind: OperationKind,
        fingerprint: str,
        outcome: MutationOutcome,
        now: datetime,
    ) -> MutationOutcome:
        return self._commit_receipt(
            self._build_receipt(operation_id, kind, fingerprint, outcome, now)
        )

    def _rate_plan(
        self, request: AdmissionRequest, now: datetime
    ) -> tuple[DenialReason | None, Decimal, Decimal]:
        ip = self._ip_rates.get(request.ip_digest)
        session = self._session_rates.get(request.session_digest)
        ip_tokens = (
            Decimal(self._config.ip_capacity)
            if ip is None
            else self._refilled(
                ip, self._config.ip_capacity, self._config.ip_refill_per_minute, now
            )
        )
        session_tokens = (
            Decimal(self._config.session_capacity)
            if session is None
            else self._refilled(
                session, self._config.session_capacity, self._config.session_refill_per_minute, now
            )
        )
        if ip_tokens < 1:
            return "ip_rate_limited", ip_tokens, session_tokens
        if session_tokens < 1:
            return "session_rate_limited", ip_tokens, session_tokens
        return None, ip_tokens, session_tokens

    def _consume_rates(
        self, request: AdmissionRequest, now: datetime, ip_tokens: Decimal, session_tokens: Decimal
    ) -> None:
        with localcontext(_CONTROL_DECIMAL_CONTEXT):
            ip_remaining = ip_tokens - Decimal(1)
            session_remaining = session_tokens - Decimal(1)
        try:
            _strict_decimal(ip_remaining, minimum=Decimal(0))
            _strict_decimal(session_remaining, minimum=Decimal(0))
        except ControlConfigurationError:
            raise ControlStoreError("malformed_store") from None
        self._ip_rates[request.ip_digest] = _RateRecord(ip_remaining, now)
        self._session_rates[request.session_digest] = _RateRecord(session_remaining, now)

    def _budget_windows(
        self, now: datetime
    ) -> tuple[str, str, _BudgetWindow | None, _BudgetWindow | None]:
        hour_key = _hour_identity(now)
        day_key = _day_identity(now)
        return hour_key, day_key, self._hours.get(hour_key), self._days.get(day_key)

    def _budget_available(self, now: datetime, amount: Decimal) -> bool:
        _, _, hour, day = self._budget_windows(now)
        if (hour is not None and hour.overrun) or (day is not None and day.overrun):
            return False
        hour_total = Decimal(0) if hour is None else _decimal_add(hour.reserved, hour.committed)
        day_total = Decimal(0) if day is None else _decimal_add(day.reserved, day.committed)
        return (
            _decimal_add(hour_total, amount) <= self._config.budget_hourly
            and _decimal_add(day_total, amount) <= self._config.budget_daily
        )

    def _reserve_budget(self, now: datetime, amount: Decimal) -> tuple[str, str]:
        hour_key, day_key, hour, day = self._budget_windows(now)
        hour_expiry, day_expiry = self._window_expiries(now)
        if hour is None:
            hour = self._hours[hour_key] = _BudgetWindow(Decimal(0), Decimal(0), hour_expiry)
        if day is None:
            day = self._days[day_key] = _BudgetWindow(Decimal(0), Decimal(0), day_expiry)
        hour.reserved = _decimal_add(hour.reserved, amount)
        day.reserved = _decimal_add(day.reserved, amount)
        return hour_key, day_key

    def _plan_lease(self, now: datetime, request: AdmissionRequest) -> tuple[ControlLease, int]:
        amount = _decimal_multiply(
            self._config.budget_reserve_per_attempt, Decimal(request.max_provider_attempts)
        )
        lease_id, fence, sequence = self._plan_token_pair()
        expiry = now + _decimal_seconds(self._config.lease_ttl_seconds)
        return (
            ControlLease(
                lease_id=lease_id,
                fencing_token=fence,
                expires_at=canonical_utc_instant(expiry),
                reservation_instant=canonical_utc_instant(now),
                attempt_date=now.date(),
                hour_window=_hour_identity(now),
                day_window=_day_identity(now),
                reserved_amount=amount,
                currency=self._config.budget_currency,
            ),
            sequence,
        )

    def _commit_lease(self, lease: ControlLease) -> None:
        instant = parse_canonical_utc_instant(lease.reservation_instant)
        hour_key, day_key = self._reserve_budget(instant, lease.reserved_amount)
        if hour_key != lease.hour_window or day_key != lease.day_window:
            raise ControlStoreError("malformed_store") from None
        self._leases[lease.lease_id] = _LeaseState(lease)

    def _plan_ticket(self, now: datetime) -> tuple[ControlTicket, int]:
        if self._sequence >= CONTROL_MAX_INTEGER:
            raise ControlStoreError("store_capacity") from None
        ticket_id, fence, sequence = self._plan_token_pair()
        expires = now + _decimal_seconds(self._config.ticket_ttl_seconds)
        return ControlTicket(ticket_id, fence, canonical_utc_instant(expires)), sequence

    def _release_reservation(
        self, lease: ControlLease, charge: Decimal, *, uncertain: bool
    ) -> None:
        hour = self._hours.get(lease.hour_window)
        day = self._days.get(lease.day_window)
        if (
            hour is None
            or day is None
            or hour.reserved < lease.reserved_amount
            or day.reserved < lease.reserved_amount
        ):
            raise ControlStoreError("malformed_store") from None
        with localcontext(_CONTROL_DECIMAL_CONTEXT):
            hour_reserved = hour.reserved - lease.reserved_amount
            day_reserved = day.reserved - lease.reserved_amount
            hour_committed = hour.committed + charge
            day_committed = day.committed + charge
        values = (hour_reserved, day_reserved, hour_committed, day_committed)
        try:
            for value in values:
                _strict_decimal(value, minimum=Decimal(0))
        except ControlConfigurationError:
            raise ControlStoreError("malformed_store") from None
        hour_overrun = (
            hour.overrun
            or charge > lease.reserved_amount
            or _decimal_add(hour_committed, hour_reserved) > self._config.budget_hourly
        )
        day_overrun = (
            day.overrun
            or charge > lease.reserved_amount
            or _decimal_add(day_committed, day_reserved) > self._config.budget_daily
        )
        hour.reserved = hour_reserved
        hour.committed = hour_committed
        hour.uncertain = hour.uncertain or uncertain
        hour.overrun = hour_overrun
        day.reserved = day_reserved
        day.committed = day_committed
        day.uncertain = day.uncertain or uncertain
        day.overrun = day_overrun

    @_detached_store_call
    async def admit_or_enqueue(
        self, operation_id: str, request: AdmissionRequest
    ) -> AdmissionOutcome:
        if not _is_hex(operation_id, 32):
            raise ControlStoreError("malformed_store") from None
        request = _copy_admission_request(request)
        if request.max_provider_attempts != self._config.maximum_provider_attempts:
            raise ControlStoreError("malformed_store") from None
        fingerprint = self._fingerprint(
            {
                "ip_digest": request.ip_digest,
                "max_provider_attempts": request.max_provider_attempts,
                "session_digest": request.session_digest,
            }
        )
        async with self._lock:
            self._preflight_receipt_collision(operation_id, "admit_or_enqueue", fingerprint)
            now = self._now()
            self._prune(now)
            self._prune_rates(now)
            replay = self._replay(operation_id, "admit_or_enqueue", fingerprint)
            if replay is not None:
                return cast(AdmissionOutcome, replay)
            rate_denial, ip_tokens, session_tokens = self._rate_plan(request, now)
            new_rates = int(request.ip_digest not in self._ip_rates) + int(
                request.session_digest not in self._session_rates
            )
            if rate_denial is not None:
                self._ensure_capacity(receipts=1)
                return cast(
                    AdmissionOutcome,
                    self._record(
                        operation_id, "admit_or_enqueue", fingerprint, Denied(rate_denial), now
                    ),
                )
            amount = _decimal_multiply(
                self._config.budget_reserve_per_attempt, Decimal(request.max_provider_attempts)
            )
            if not self._budget_available(now, amount):
                outcome: AdmissionOutcome = Denied("budget_exhausted")
                extra_tickets = extra_leases = extra_budgets = 0
            elif len(self._leases) < self._config.max_active_requests and not self._tickets:
                planned_lease, planned_token_sequence = self._plan_lease(now, request)
                outcome = Admitted(planned_lease)
                extra_tickets = 0
                extra_leases = 1
                hour_key, day_key, hour, day = self._budget_windows(now)
                del hour_key, day_key
                extra_budgets = int(hour is None) + int(day is None)
            elif (
                self._config.max_queued_requests > len(self._tickets)
                and self._config.queue_wait_seconds > 0
            ):
                planned_ticket, planned_token_sequence = self._plan_ticket(now)
                outcome = Queued(planned_ticket)
                extra_tickets = 1
                extra_leases = extra_budgets = 0
            else:
                outcome = Denied("concurrency_limited")
                extra_tickets = extra_leases = extra_budgets = 0
            self._ensure_capacity(
                rates=new_rates,
                tickets=extra_tickets,
                leases=extra_leases,
                budgets=extra_budgets,
                receipts=1,
                future_receipt_delta=(
                    CONTROL_MAX_RENEWAL_TICKS_PER_LEASE + 1
                    if type(outcome) is Admitted
                    else self._config.poll_attempts + 1
                    if type(outcome) is Queued
                    else 0
                ),
            )
            receipt = self._build_receipt(
                operation_id,
                "admit_or_enqueue",
                fingerprint,
                outcome,
                now,
            )
            self._consume_rates(request, now, ip_tokens, session_tokens)
            if type(outcome) is Admitted:
                self._token_sequence = planned_token_sequence
                self._commit_lease(outcome.lease)
            elif type(outcome) is Queued:
                self._token_sequence = planned_token_sequence
                self._sequence += 1
                self._tickets[outcome.ticket.ticket_id] = _TicketState(
                    outcome.ticket,
                    request,
                    self._sequence,
                    self._config.poll_attempts,
                )
            return cast(AdmissionOutcome, self._commit_receipt(receipt))

    @_detached_store_call
    async def wait_for_admission(
        self, operation_id: str, ticket_id: str, fencing_token: str
    ) -> PollOutcome:
        if (
            not _is_hex(operation_id, 32)
            or not _is_hex(ticket_id, 32)
            or not _is_hex(fencing_token, 32)
        ):
            raise ControlStoreError("malformed_store") from None
        fingerprint = self._fingerprint({"fencing_token": fencing_token, "ticket_id": ticket_id})
        async with self._lock:
            self._preflight_receipt_collision(operation_id, "wait_for_admission", fingerprint)
            now = self._now()
            self._prune(now)
            replay = self._replay(operation_id, "wait_for_admission", fingerprint)
            if replay is not None:
                return cast(PollOutcome, replay)
            state = self._tickets.get(ticket_id)
            if state is None:
                self._ensure_capacity(receipts=1)
                outcome: PollOutcome = Denied("concurrency_limited")
            elif state.ticket.fencing_token != fencing_token:
                raise ControlStoreError("malformed_store") from None
            elif state.polls_remaining <= 0:
                raise ControlStoreError("store_capacity") from None
            elif state.sequence != min(item.sequence for item in self._tickets.values()):
                outcome = Pending(state.ticket)
            elif len(self._leases) >= self._config.max_active_requests:
                outcome = Pending(state.ticket)
            else:
                amount = _decimal_multiply(
                    self._config.budget_reserve_per_attempt,
                    Decimal(state.request.max_provider_attempts),
                )
                if not self._budget_available(now, amount):
                    outcome = Denied("budget_exhausted")
                else:
                    hour_key, day_key, hour, day = self._budget_windows(now)
                    del hour_key, day_key
                    extra_budgets = int(hour is None) + int(day is None)
                    planned_lease, planned_token_sequence = self._plan_lease(now, state.request)
                    outcome = Admitted(planned_lease)
            if state is not None:
                future_delta = -(state.polls_remaining + 1)
                if type(outcome) is Pending:
                    future_delta = -1
                elif type(outcome) is Admitted:
                    future_delta += CONTROL_MAX_RENEWAL_TICKS_PER_LEASE + 1
                self._ensure_capacity(
                    tickets=-1 if type(outcome) is not Pending else 0,
                    leases=1 if type(outcome) is Admitted else 0,
                    budgets=extra_budgets if type(outcome) is Admitted else 0,
                    receipts=1,
                    future_receipt_delta=future_delta,
                )
            receipt = self._build_receipt(
                operation_id,
                "wait_for_admission",
                fingerprint,
                outcome,
                now,
            )
            if type(outcome) is Denied and state is not None:
                del self._tickets[ticket_id]
            elif type(outcome) is Admitted:
                del self._tickets[ticket_id]
                self._token_sequence = planned_token_sequence
                self._commit_lease(outcome.lease)
            elif type(outcome) is Pending and state is not None:
                state.polls_remaining -= 1
            return cast(PollOutcome, self._commit_receipt(receipt))

    @_detached_store_call
    async def renew(self, operation_id: str, lease_id: str, fencing_token: str) -> RenewOutcome:
        if (
            not _is_hex(operation_id, 32)
            or not _is_hex(lease_id, 32)
            or not _is_hex(fencing_token, 32)
        ):
            raise ControlStoreError("malformed_store") from None
        fingerprint = self._fingerprint({"fencing_token": fencing_token, "lease_id": lease_id})
        async with self._lock:
            self._preflight_receipt_collision(operation_id, "renew", fingerprint)
            now = self._now()
            self._prune(now)
            replay = self._replay(operation_id, "renew", fingerprint)
            if replay is not None:
                return cast(RenewOutcome, replay)
            state = self._leases.get(lease_id)
            if state is None:
                self._ensure_capacity(receipts=1)
                outcome: RenewOutcome = Lost("lease_expired")
            elif state.lease.fencing_token != fencing_token:
                self._ensure_capacity(receipts=1)
                outcome = Lost("fencing_lost")
            elif state.renewals_remaining <= 0:
                raise ControlStoreError("store_capacity") from None
            else:
                self._ensure_capacity(receipts=1, future_receipt_delta=-1)
                expiry = now + _decimal_seconds(self._config.lease_ttl_seconds)
                renewed_lease = ControlLease(
                    lease_id=state.lease.lease_id,
                    fencing_token=state.lease.fencing_token,
                    expires_at=canonical_utc_instant(expiry),
                    reservation_instant=state.lease.reservation_instant,
                    attempt_date=state.lease.attempt_date,
                    hour_window=state.lease.hour_window,
                    day_window=state.lease.day_window,
                    reserved_amount=state.lease.reserved_amount,
                    currency=state.lease.currency,
                )
                outcome = Renewed(renewed_lease.expires_at)
            receipt = self._build_receipt(
                operation_id,
                "renew",
                fingerprint,
                outcome,
                now,
            )
            if state is not None and type(outcome) is Renewed:
                state.lease = renewed_lease
                state.renewals_remaining -= 1
            return cast(RenewOutcome, self._commit_receipt(receipt))

    @_detached_store_call
    async def finalize(
        self,
        operation_id: str,
        lease_id: str,
        fencing_token: str,
        reconciliation: BudgetReconciliation,
    ) -> Finalized:
        if (
            not _is_hex(operation_id, 32)
            or not _is_hex(lease_id, 32)
            or not _is_hex(fencing_token, 32)
        ):
            raise ControlStoreError("malformed_store") from None
        reconciliation = _copy_reconciliation(reconciliation)
        fingerprint = self._fingerprint(
            {
                "accounting_uncertain": reconciliation.accounting_uncertain,
                "charge": format(reconciliation.charge, "f"),
                "currency": reconciliation.currency,
                "fencing_token": fencing_token,
                "lease_id": lease_id,
            }
        )
        async with self._lock:
            self._preflight_receipt_collision(operation_id, "finalize", fingerprint)
            now = self._now()
            self._prune(now)
            replay = self._replay(operation_id, "finalize", fingerprint)
            if replay is not None:
                return cast(Finalized, replay)
            state = self._leases.get(lease_id)
            if state is None or state.lease.fencing_token != fencing_token:
                raise ControlStoreError("fencing_lost") from None
            if reconciliation.currency != state.lease.currency:
                raise ControlStoreError("malformed_store") from None
            self._ensure_capacity(
                receipts=1,
                future_receipt_delta=-(state.renewals_remaining + 1),
            )
            result = Finalized()
            receipt = self._build_receipt(
                operation_id,
                "finalize",
                fingerprint,
                result,
                now,
            )
            self._release_reservation(
                state.lease, reconciliation.charge, uncertain=reconciliation.accounting_uncertain
            )
            del self._leases[lease_id]
            return cast(Finalized, self._commit_receipt(receipt))

    @_detached_store_call
    async def cancel_ticket(
        self, operation_id: str, ticket_id: str, fencing_token: str
    ) -> CancelledTicket:
        if (
            not _is_hex(operation_id, 32)
            or not _is_hex(ticket_id, 32)
            or not _is_hex(fencing_token, 32)
        ):
            raise ControlStoreError("malformed_store") from None
        fingerprint = self._fingerprint({"fencing_token": fencing_token, "ticket_id": ticket_id})
        async with self._lock:
            self._preflight_receipt_collision(operation_id, "cancel_ticket", fingerprint)
            now = self._now()
            self._prune(now)
            replay = self._replay(operation_id, "cancel_ticket", fingerprint)
            if replay is not None:
                return cast(CancelledTicket, replay)
            state = self._tickets.get(ticket_id)
            if state is not None:
                if state.ticket.fencing_token != fencing_token:
                    raise ControlStoreError("malformed_store") from None
                self._ensure_capacity(
                    receipts=1,
                    future_receipt_delta=-(state.polls_remaining + 1),
                )
            else:
                self._ensure_capacity(receipts=1)
            result = CancelledTicket()
            receipt = self._build_receipt(
                operation_id,
                "cancel_ticket",
                fingerprint,
                result,
                now,
            )
            if state is not None:
                del self._tickets[ticket_id]
            return cast(CancelledTicket, self._commit_receipt(receipt))

    @_detached_store_call
    async def check_budget_readiness(self) -> object:
        from app.readiness import BudgetReadiness

        async with self._lock:
            now = self._now()
            self._prune(now)
            hour_key, day_key, hour, day = self._budget_windows(now)
            del hour_key, day_key
            windows = tuple(item for item in (hour, day) if item is not None)
            if any(item.overrun for item in windows):
                return BudgetReadiness(state="not_ready", reason="budget_overrun")
            if any(item.uncertain for item in windows):
                return BudgetReadiness(state="unknown", reason="accounting_uncertain")
            next_request_reserve = _decimal_multiply(
                self._config.budget_reserve_per_attempt,
                Decimal(self._config.maximum_provider_attempts),
            )
            if not self._budget_available(now, next_request_reserve):
                return BudgetReadiness(state="not_ready", reason="budget_exhausted")
            return BudgetReadiness(state="ready", reason="ready")

    def snapshot(self) -> dict[str, Any]:
        budget: dict[str, dict[str, Decimal | bool]] = {}
        for key, value in self._hours.items():
            budget[key] = {
                "reserved": value.reserved,
                "committed": value.committed,
                "uncertain": value.uncertain,
                "overrun": value.overrun,
            }
        for key, value in self._days.items():
            budget[key] = {
                "reserved": value.reserved,
                "committed": value.committed,
                "uncertain": value.uncertain,
                "overrun": value.overrun,
            }
        return {
            "counts": {
                "ip_rates": len(self._ip_rates),
                "session_rates": len(self._session_rates),
                "tickets": len(self._tickets),
                "leases": len(self._leases),
                "budget_windows": len(self._hours) + len(self._days),
                "receipts": len(self._receipts),
                "total": self._count(),
            },
            "budget": budget,
        }


def _reconciliation_from_summary_inner(
    summary: object,
    *,
    reserve_per_attempt: Decimal,
    currency: str,
) -> BudgetReconciliation:
    if (
        type(summary) is not RequestAccountingSummary
        or type(reserve_per_attempt) is not Decimal
        or type(currency) is not str
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isupper()
        or not currency.isalpha()
    ):
        raise EndpointControllerError from None
    try:
        _strict_decimal(reserve_per_attempt, minimum=Decimal(0))
    except ControlConfigurationError:
        raise EndpointControllerError from None
    try:
        copied = summary.model_copy()
        attempts = object.__getattribute__(copied, "attempts")
        started = object.__getattribute__(copied, "provider_work_started")
        completion = object.__getattribute__(copied, "request_completion")
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError):
        raise
    except Exception:
        raise EndpointControllerError from None
    if type(attempts) is not tuple or type(started) is not bool or started is not bool(attempts):
        raise EndpointControllerError from None
    charge = Decimal(0)
    uncertain = False
    for attempt in attempts:
        if (
            completion != "abandoned"
            and type(attempt) is SettledAttempt
            and attempt.cost_record.cost_state == "priced"
            and attempt.cost_record.model_cost is not None
            and attempt.cost_record.currency == currency
        ):
            charge = _decimal_add(charge, attempt.cost_record.model_cost)
        else:
            charge = _decimal_add(charge, reserve_per_attempt)
            uncertain = True
    try:
        return BudgetReconciliation(
            charge=charge, currency=currency, accounting_uncertain=uncertain
        )
    except ControlStoreError:
        raise EndpointControllerError from None


def reconciliation_from_summary(
    summary: object,
    *,
    reserve_per_attempt: Decimal,
    currency: str,
) -> BudgetReconciliation:
    failed = False
    primary: BaseException | None = None
    try:
        result = _reconciliation_from_summary_inner(
            summary,
            reserve_per_attempt=reserve_per_attempt,
            currency=currency,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as error:
        primary = error
    except BaseException as error:
        _release_exception(error)
        failed = True
    if primary is not None:
        exact_primary = primary
        del summary, reserve_per_attempt, currency, primary
        raise exact_primary.with_traceback(None) from None
    if failed:
        del summary, reserve_per_attempt, currency
        raise EndpointControllerError from None
    return result


async def _cancel_and_drain(
    *futures: asyncio.Future[object],
) -> BaseException | None:
    primary: BaseException | None = None
    if _caller_is_cancelling():
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            primary = error
    for future in futures:
        if not future.done():
            future.cancel()
    for future in futures:
        try:
            await future
        except asyncio.CancelledError as error:
            if primary is None and _caller_is_cancelling():
                primary = error
            else:
                _release_exception(error)
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            if primary is None:
                primary = error
            else:
                _release_exception(error)
        except BaseException as error:
            _release_exception(error)
    return primary


def _release_exception(error: BaseException) -> None:
    BaseException.__setattr__(error, "__traceback__", None)
    BaseException.__setattr__(error, "__cause__", None)
    BaseException.__setattr__(error, "__context__", None)


def _caller_is_cancelling() -> bool:
    try:
        current = asyncio.current_task()
    except RuntimeError:
        return False
    return current is not None and current.cancelling() > 0


def _detached_controller_call[**P, R](
    operation: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(operation)
    async def detached(
        *args: P.args, **kwargs: P.kwargs
    ) -> R:
        failure: str | None = None
        store_code: ControlErrorCode = "malformed_store"
        primary: BaseException | None = None
        try:
            result = await operation(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as error:
            primary = error
        except BaseException as error:
            if type(error) is ControlStoreError:
                failure = "store"
                store_code = _normalized_store_error(error).code
            elif type(error) is ClientPeerError:
                failure = "peer"
            elif type(error) is ClientIdentityError:
                failure = "identity"
            else:
                failure = "controller"
            _release_exception(error)
        if primary is not None:
            exact_primary = primary
            del args, kwargs, primary
            raise exact_primary.with_traceback(None) from None
        if failure is not None:
            kind = failure
            del args, kwargs, failure
            if kind == "store":
                raise ControlStoreError(store_code) from None
            if kind == "peer":
                raise ClientPeerError from None
            if kind == "identity":
                raise ClientIdentityError from None
            raise EndpointControllerError from None
        return result

    return detached


def _detached_controller_sync_call[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    @wraps(operation)
    def detached(*args: P.args, **kwargs: P.kwargs) -> R:
        failure: str | None = None
        store_code: ControlErrorCode = "malformed_store"
        primary: BaseException | None = None
        try:
            result = operation(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as error:
            primary = error
        except BaseException as error:
            if type(error) is ControlStoreError:
                failure = "store"
                store_code = _normalized_store_error(error).code
            elif type(error) is ClientPeerError:
                failure = "peer"
            elif type(error) is ClientIdentityError:
                failure = "identity"
            else:
                failure = "controller"
            _release_exception(error)
        if primary is not None:
            exact_primary = primary
            del args, kwargs, primary
            raise exact_primary.with_traceback(None) from None
        if failure is not None:
            kind = failure
            del args, kwargs, failure
            if kind == "store":
                raise ControlStoreError(store_code) from None
            if kind == "peer":
                raise ClientPeerError from None
            if kind == "identity":
                raise ClientIdentityError from None
            raise EndpointControllerError from None
        return result

    return detached


async def _await_bounded[T](
    operation: Callable[[], Awaitable[T]],
    timeout: float = CONTROL_STORE_CALL_TIMEOUT_SECONDS,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    if type(timeout) is not float or not 0 < timeout <= CONTROL_STORE_CALL_TIMEOUT_SECONDS:
        raise ControlStoreError("controller_invariant") from None
    clock_failed = False
    try:
        started = monotonic()
    except asyncio.CancelledError as error:
        if _caller_is_cancelling():
            raise
        _release_exception(error)
        clock_failed = True
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception as error:
        _release_exception(error)
        clock_failed = True
    if clock_failed:
        del operation, monotonic, sleeper
        raise ControlStoreError("controller_invariant") from None
    if type(started) is not float or not math.isfinite(started):
        del operation, monotonic, sleeper
        raise ControlStoreError("controller_invariant") from None
    deadline = started + timeout
    if not math.isfinite(deadline) or deadline < started:
        del operation, monotonic, sleeper
        raise ControlStoreError("controller_invariant") from None

    timer_setup_failed = False
    try:
        timer: asyncio.Future[None] = asyncio.ensure_future(sleeper(timeout))
    except asyncio.CancelledError as error:
        if _caller_is_cancelling():
            raise
        _release_exception(error)
        timer_setup_failed = True
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception as error:
        _release_exception(error)
        timer_setup_failed = True
    if timer_setup_failed:
        del operation, monotonic, sleeper
        raise ControlStoreError("controller_invariant") from None

    operation_setup_failed = False
    operation_primary: BaseException | None = None
    try:
        awaitable = operation()
        task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
    except asyncio.CancelledError as error:
        if _caller_is_cancelling():
            operation_primary = error
        else:
            _release_exception(error)
            operation_setup_failed = True
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
        operation_primary = error
    except Exception as error:
        _release_exception(error)
        operation_setup_failed = True
    if operation_primary is not None:
        await _cancel_and_drain(cast(asyncio.Future[object], timer))
        primary = operation_primary
        del operation, monotonic, sleeper, timer, operation_primary
        raise primary.with_traceback(None) from None
    if operation_setup_failed:
        cleanup_primary = await _cancel_and_drain(cast(asyncio.Future[object], timer))
        if cleanup_primary is not None:
            primary = cleanup_primary
            del operation, monotonic, sleeper, timer, cleanup_primary
            raise primary.with_traceback(None) from None
        del operation, monotonic, sleeper, timer
        raise ControlStoreError("malformed_store") from None

    caller_cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError as error:
        caller_cancellation = error
    if caller_cancellation is not None:
        await _cancel_and_drain(
            cast(asyncio.Future[object], task),
            cast(asyncio.Future[object], timer),
        )
        primary_cancellation = caller_cancellation
        del operation, monotonic, sleeper, task, timer, awaitable, caller_cancellation
        raise primary_cancellation.with_traceback(None) from None

    clock_failed = False
    clock_primary: BaseException | None = None
    try:
        now = monotonic()
    except asyncio.CancelledError as error:
        if _caller_is_cancelling():
            clock_primary = error
        else:
            _release_exception(error)
            clock_failed = True
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
        clock_primary = error
    except Exception as error:
        _release_exception(error)
        clock_failed = True
    if clock_primary is not None:
        await _cancel_and_drain(
            cast(asyncio.Future[object], task),
            cast(asyncio.Future[object], timer),
        )
        primary = clock_primary
        del operation, monotonic, sleeper, task, timer, awaitable, clock_primary
        raise primary.with_traceback(None) from None
    if clock_failed or type(now) is not float or not math.isfinite(now):
        cleanup_primary = await _cancel_and_drain(
            cast(asyncio.Future[object], task),
            cast(asyncio.Future[object], timer),
        )
        if cleanup_primary is not None:
            primary = cleanup_primary
            del operation, monotonic, sleeper, task, timer, awaitable, cleanup_primary
            raise primary.with_traceback(None) from None
        del operation, monotonic, sleeper, task, timer, awaitable
        raise ControlStoreError("controller_invariant") from None

    timer_failed = False
    timer_primary: BaseException | None = None
    if timer.done():
        try:
            timer.result()
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                timer_primary = error
            else:
                _release_exception(error)
                timer_failed = True
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            timer_primary = error
        except Exception as error:
            _release_exception(error)
            timer_failed = True
    if timer_primary is not None:
        await _cancel_and_drain(cast(asyncio.Future[object], task))
        primary = timer_primary
        del operation, monotonic, sleeper, task, timer, awaitable, timer_primary
        raise primary.with_traceback(None) from None
    if timer_failed:
        cleanup_primary = await _cancel_and_drain(cast(asyncio.Future[object], task))
        if cleanup_primary is not None:
            primary = cleanup_primary
            del operation, monotonic, sleeper, task, timer, awaitable, cleanup_primary
            raise primary.with_traceback(None) from None
        del operation, monotonic, sleeper, task, timer, awaitable
        raise ControlStoreError("controller_invariant") from None
    if timer.done() and now < deadline:
        cleanup_primary = await _cancel_and_drain(cast(asyncio.Future[object], task))
        if cleanup_primary is not None:
            primary = cleanup_primary
            del operation, monotonic, sleeper, task, timer, awaitable, cleanup_primary
            raise primary.with_traceback(None) from None
        del operation, monotonic, sleeper, task, timer, awaitable
        raise ControlStoreError("controller_invariant") from None

    if task.done() and now < deadline and not timer.done():
        cleanup_primary = await _cancel_and_drain(cast(asyncio.Future[object], timer))
        if cleanup_primary is not None:
            primary = cleanup_primary
            del operation, monotonic, sleeper, task, timer, awaitable, cleanup_primary
            raise primary.with_traceback(None) from None
        store_code: ControlErrorCode | None = None
        store_primary: BaseException | None = None
        try:
            result = task.result()
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                store_primary = error
            else:
                _release_exception(error)
                store_code = "store_unavailable"
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            store_primary = error
        except ControlStoreError as error:
            normalized = _normalized_store_error(error)
            store_code = normalized.code
            _release_exception(error)
        except Exception as error:
            _release_exception(error)
            store_code = "store_unavailable"
        if store_primary is not None:
            primary = store_primary
            del operation, monotonic, sleeper, task, timer, awaitable, store_primary
            raise primary.with_traceback(None) from None
        if store_code is not None:
            code = store_code
            del operation, monotonic, sleeper, task, timer, awaitable, store_code
            raise ControlStoreError(code) from None
        del operation, monotonic, sleeper, task, timer, awaitable
        return result

    cleanup_primary = await _cancel_and_drain(
        cast(asyncio.Future[object], task),
        cast(asyncio.Future[object], timer),
    )
    if cleanup_primary is not None:
        primary = cleanup_primary
        del operation, monotonic, sleeper, task, timer, awaitable, cleanup_primary
        raise primary.with_traceback(None) from None
    del operation, monotonic, sleeper, task, timer, awaitable
    raise ControlStoreError("store_timeout") from None


def new_operation_id() -> str:
    return secrets.token_hex(16)


async def call_mutation_with_one_replay[T](
    operation: Callable[[], Awaitable[T]],
    *,
    timeout: float = CONTROL_STORE_CALL_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    overall_deadline: float | None = None,
) -> T:
    """Replay one unknown/timeout mutation using the caller-captured same ID."""

    try:
        first_timeout = timeout
        if overall_deadline is not None:
            first_timeout = min(first_timeout, overall_deadline - monotonic())
        return await _await_bounded(operation, first_timeout, monotonic=monotonic, sleeper=sleeper)
    except asyncio.CancelledError as error:
        primary = error
        del operation, timeout, monotonic, sleeper, overall_deadline, first_timeout
        raise primary.with_traceback(None) from None
    except ControlStoreError as error:
        normalized = _normalized_store_error(error)
        del error
        if normalized.code not in {"store_timeout", "unknown_outcome"}:
            failure = normalized
            del operation, timeout, monotonic, sleeper, overall_deadline, first_timeout
            del normalized
            raise failure from None
    try:
        replay_timeout = timeout
        if overall_deadline is not None:
            replay_timeout = min(replay_timeout, overall_deadline - monotonic())
        return await _await_bounded(operation, replay_timeout, monotonic=monotonic, sleeper=sleeper)
    except asyncio.CancelledError as error:
        primary = error
        del operation, timeout, monotonic, sleeper, overall_deadline, replay_timeout
        raise primary.with_traceback(None) from None
    except ControlStoreError as error:
        normalized = _normalized_store_error(error)
        del error
        if normalized.code in {"store_timeout", "unknown_outcome"}:
            del operation, timeout, monotonic, sleeper, overall_deadline, replay_timeout
            del normalized
            raise ControlStoreError("unknown_outcome") from None
        failure = normalized
        del operation, timeout, monotonic, sleeper, overall_deadline, replay_timeout
        del normalized
        raise failure from None


class EndpointController(metaclass=_DetachedConfigMeta):
    """Controller-owned IDs/deadlines around one borrowed atomic store."""

    def __init__(
        self,
        store: EndpointControlStore,
        config: EndpointControlConfig,
        *,
        operation_id_factory: Callable[[], str] = new_operation_id,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            not _has_async_store_shape(store)
            or type(config) is not EndpointControlConfig
            or not callable(operation_id_factory)
            or not callable(monotonic)
            or not callable(sleeper)
        ):
            raise ControlConfigurationError from None
        self._store = store
        self._config = _validated_config(config)
        self._operation_id_factory = operation_id_factory
        self._operation_sequence = 0
        self._monotonic = monotonic
        self._last_monotonic: float | None = None
        self._sleeper = sleeper
        self._lease_deadlines: dict[tuple[str, str], float] = {}
        self._lease_store_expiries: dict[tuple[str, str], datetime] = {}

    @_detached_controller_sync_call
    def monotonic(self) -> float:
        failed = False
        try:
            value: object = self._monotonic()
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                raise
            _release_exception(error)
            failed = True
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as error:
            _release_exception(error)
            failed = True
        if failed:
            raise EndpointControllerError from None
        if type(value) is types.CoroutineType:
            value.close()
            raise EndpointControllerError from None
        if (
            type(value) is not float
            or not math.isfinite(value)
            or (self._last_monotonic is not None and value < self._last_monotonic)
        ):
            raise EndpointControllerError from None
        monotonic_value = value
        self._last_monotonic = monotonic_value
        return monotonic_value

    @staticmethod
    @_detached_controller_sync_call
    def deadline_from(start: float, duration: float) -> float:
        if (
            type(start) is not float
            or type(duration) is not float
            or not math.isfinite(start)
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise EndpointControllerError from None
        deadline = start + duration
        if not math.isfinite(deadline) or deadline < start:
            raise EndpointControllerError from None
        return deadline

    @_detached_controller_call
    async def sleep(self, delay: float) -> None:
        if type(delay) is not float or not math.isfinite(delay) or delay < 0:
            raise EndpointControllerError from None
        started = self.monotonic()
        deadline = self.deadline_from(started, delay)
        try:
            await self._sleeper(delay)
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                raise
            _release_exception(error)
            raise EndpointControllerError from None
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as error:
            _release_exception(error)
            raise EndpointControllerError from None
        if self.monotonic() < deadline:
            raise EndpointControllerError from None

    @property
    def queue_wait_seconds(self) -> float:
        return float(self._config.queue_wait_seconds)

    @property
    def lease_renew_seconds(self) -> float:
        return float(self._config.lease_renew_seconds)

    @_detached_controller_sync_call
    def authority_remaining(self, lease: ControlLease) -> float:
        lease = _copy_lease(lease)
        key = (lease.lease_id, lease.fencing_token)
        deadline = self._lease_deadlines.get(key)
        if deadline is None:
            return 0.0
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            self._lease_deadlines.pop(key, None)
            self._lease_store_expiries.pop(key, None)
            return 0.0
        return remaining

    def _record_authority(self, lease: ControlLease, started: float) -> None:
        for key, deadline in tuple(self._lease_deadlines.items()):
            if deadline <= started:
                self._lease_deadlines.pop(key, None)
                self._lease_store_expiries.pop(key, None)
        if len(self._lease_deadlines) >= self._config.max_active_requests or any(
            key[0] == lease.lease_id for key in self._lease_deadlines
        ):
            raise ControlStoreError("malformed_store") from None
        key = (lease.lease_id, lease.fencing_token)
        self._lease_deadlines[key] = self.deadline_from(
            started, float(self._config.lease_ttl_seconds)
        )
        self._lease_store_expiries[key] = parse_canonical_utc_instant(lease.expires_at)

    def _revoke_authority(self, lease: ControlLease) -> tuple[str, str]:
        key = (lease.lease_id, lease.fencing_token)
        self._lease_deadlines.pop(key, None)
        self._lease_store_expiries.pop(key, None)
        return key

    def _validated_store_lease(self, lease: ControlLease) -> ControlLease:
        lease = _copy_lease(lease)
        reservation = parse_canonical_utc_instant(lease.reservation_instant)
        expected_expiry = reservation + _decimal_seconds(self._config.lease_ttl_seconds)
        expected_reserve = _decimal_multiply(
            self._config.budget_reserve_per_attempt,
            Decimal(self._config.maximum_provider_attempts),
        )
        if (
            lease.expires_at != canonical_utc_instant(expected_expiry)
            or lease.reserved_amount != expected_reserve
            or lease.currency != self._config.budget_currency
        ):
            raise ControlStoreError("malformed_store") from None
        return lease

    async def _mutation[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        timeout: float = CONTROL_STORE_CALL_TIMEOUT_SECONDS,
        overall_deadline: float | None = None,
    ) -> T:
        return await call_mutation_with_one_replay(
            operation,
            timeout=timeout,
            monotonic=self.monotonic,
            sleeper=self._sleeper,
            overall_deadline=overall_deadline,
        )

    def _id(self) -> str:
        failed = False
        try:
            raw_value: object = self._operation_id_factory()
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                raise
            _release_exception(error)
            failed = True
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as error:
            _release_exception(error)
            failed = True
        if failed:
            raise EndpointControllerError from None
        if type(raw_value) is types.CoroutineType:
            raw_value.close()
            raise EndpointControllerError from None
        if not _is_hex(raw_value, 32) or self._operation_sequence >= CONTROL_MAX_INTEGER:
            raise EndpointControllerError from None
        self._operation_sequence += 1
        return hashlib.sha256(
            bytes.fromhex(cast(str, raw_value)) + self._operation_sequence.to_bytes(8, "big")
        ).hexdigest()[:32]

    def _store_method(self, name: str) -> Callable[..., Awaitable[object]]:
        failed = False
        try:
            descriptor = inspect.getattr_static(self._store, name)
            method = getattr(self._store, name)
        except asyncio.CancelledError as error:
            if _caller_is_cancelling():
                raise
            _release_exception(error)
            failed = True
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as error:
            _release_exception(error)
            failed = True
        if failed:
            raise ControlStoreError("malformed_store") from None
        if (
            type(descriptor) is not types.FunctionType
            or not inspect.iscoroutinefunction(descriptor)
            or type(method) is not types.MethodType
        ):
            raise ControlStoreError("malformed_store") from None
        return cast(Callable[..., Awaitable[object]], method)

    @_detached_controller_call
    async def admit(
        self,
        peer: object,
        headers: Iterable[tuple[bytes, bytes]],
        session_id: object,
        max_provider_attempts: int,
    ) -> AdmissionOutcome:
        identity = resolve_client_identity(peer, headers, self._config.trusted_proxy_cidrs)
        if type(session_id) is not str:
            raise EndpointControllerError from None
        request = AdmissionRequest(
            ip_digest=opaque_identity_digest(self._config.digest_key, "ip", identity),
            session_digest=opaque_identity_digest(self._config.digest_key, "session", session_id),
            max_provider_attempts=max_provider_attempts,
        )
        request_values = (
            request.ip_digest,
            request.session_digest,
            request.max_provider_attempts,
        )
        method = self._store_method("admit_or_enqueue")
        operation_id = self._id()
        started = self.monotonic()
        copied = cast(
            AdmissionOutcome,
            _normalized_outcome(
                "admit_or_enqueue",
                await self._mutation(
                    lambda: method(operation_id, AdmissionRequest(*request_values))
                ),
            ),
        )
        if type(copied) is Admitted:
            copied = Admitted(self._validated_store_lease(copied.lease))
            self._record_authority(copied.lease, started)
        return copied

    @_detached_controller_call
    async def poll(
        self, ticket: ControlTicket, *, overall_deadline: float | None = None
    ) -> PollOutcome:
        ticket = _copy_ticket(ticket)
        ticket_values = (ticket.ticket_id, ticket.fencing_token)
        method = self._store_method("wait_for_admission")
        operation_id = self._id()
        started = self.monotonic()
        timeout = CONTROL_STORE_CALL_TIMEOUT_SECONDS
        if overall_deadline is not None:
            timeout = min(timeout, overall_deadline - started)
        copied = cast(
            PollOutcome,
            _normalized_outcome(
                "wait_for_admission",
                await self._mutation(
                    lambda: method(operation_id, *ticket_values),
                    timeout=timeout,
                    overall_deadline=overall_deadline,
                ),
            ),
        )
        if type(copied) is Pending and copied.ticket != ticket:
            raise ControlStoreError("malformed_store") from None
        if type(copied) is Admitted:
            copied = Admitted(self._validated_store_lease(copied.lease))
            self._record_authority(copied.lease, started)
        return copied

    @_detached_controller_call
    async def cancel(self, ticket: ControlTicket) -> None:
        ticket = _copy_ticket(ticket)
        ticket_values = (ticket.ticket_id, ticket.fencing_token)
        method = self._store_method("cancel_ticket")
        operation_id = self._id()
        _normalized_outcome(
            "cancel_ticket", await self._mutation(lambda: method(operation_id, *ticket_values))
        )

    @_detached_controller_call
    async def finalize(self, lease: ControlLease, summary: RequestAccountingSummary) -> None:
        lease = _copy_lease(lease)
        self._revoke_authority(lease)
        reconciliation = reconciliation_from_summary(
            summary,
            reserve_per_attempt=self._config.budget_reserve_per_attempt,
            currency=self._config.budget_currency,
        )
        reconciliation_values = (
            reconciliation.charge,
            reconciliation.currency,
            reconciliation.accounting_uncertain,
        )
        method = self._store_method("finalize")
        operation_id = self._id()
        _normalized_outcome(
            "finalize",
            await self._mutation(
                lambda: method(
                    operation_id,
                    lease.lease_id,
                    lease.fencing_token,
                    BudgetReconciliation(*reconciliation_values),
                )
            ),
        )

    @_detached_controller_call
    async def finalize_without_provider(self, lease: ControlLease) -> None:
        lease = _copy_lease(lease)
        self._revoke_authority(lease)
        operation_id = self._id()
        method = self._store_method("finalize")
        _normalized_outcome(
            "finalize",
            await self._mutation(
                lambda: method(
                    operation_id,
                    lease.lease_id,
                    lease.fencing_token,
                    BudgetReconciliation(Decimal(0), self._config.budget_currency, False),
                )
            ),
        )

    @_detached_controller_call
    async def finalize_uncertain(self, lease: ControlLease) -> None:
        lease = _copy_lease(lease)
        self._revoke_authority(lease)
        operation_id = self._id()
        method = self._store_method("finalize")
        _normalized_outcome(
            "finalize",
            await self._mutation(
                lambda: method(
                    operation_id,
                    lease.lease_id,
                    lease.fencing_token,
                    BudgetReconciliation(
                        lease.reserved_amount,
                        self._config.budget_currency,
                        True,
                    ),
                )
            ),
        )

    @_detached_controller_call
    async def renew(self, lease: ControlLease) -> RenewOutcome:
        lease = _copy_lease(lease)
        key = (lease.lease_id, lease.fencing_token)
        authority_renewed = False
        try:
            started = self.monotonic()
            deadline = self._lease_deadlines.get(key)
            store_expiry = self._lease_store_expiries.get(key)
            if (
                deadline is None
                or store_expiry is None
                or deadline - started < 2 * CONTROL_STORE_CALL_TIMEOUT_SECONDS
            ):
                return Lost("lease_expired")
            operation_id = self._id()
            method = self._store_method("renew")
            copied = cast(
                RenewOutcome,
                _normalized_outcome(
                    "renew",
                    await self._mutation(
                        lambda: method(operation_id, lease.lease_id, lease.fencing_token),
                        overall_deadline=deadline,
                    ),
                ),
            )
            if type(copied) is Renewed:
                renewed_expiry = parse_canonical_utc_instant(copied.expires_at)
                actual_advance = Decimal(
                    (renewed_expiry - store_expiry).days * 86_400
                    + (renewed_expiry - store_expiry).seconds
                )
                with localcontext(_CONTROL_DECIMAL_CONTEXT):
                    actual_advance += Decimal(
                        (renewed_expiry - store_expiry).microseconds
                    ) / Decimal(1_000_000)
                if actual_advance <= 0:
                    raise ControlStoreError("malformed_store") from None
                advance = float(actual_advance)
                advanced_proxy = self.deadline_from(deadline, advance)
                full_deadline = self.deadline_from(
                    started, float(self._config.lease_ttl_seconds)
                )
                new_deadline = min(advanced_proxy, full_deadline)
                if new_deadline <= started:
                    raise ControlStoreError("malformed_store") from None
                self._lease_deadlines[key] = new_deadline
                self._lease_store_expiries[key] = renewed_expiry
                authority_renewed = True
            return copied
        finally:
            if not authority_renewed:
                self._revoke_authority(lease)

    @_detached_controller_call
    async def check_readiness(self) -> object:
        method = self._store_method("check_budget_readiness")
        result = await _await_bounded(
            method,
            monotonic=self.monotonic,
            sleeper=self._sleeper,
        )
        if type(result) is types.CoroutineType:
            result.close()
            raise ControlStoreError("malformed_store") from None
        return result
