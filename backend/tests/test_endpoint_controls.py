from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import weakref
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Context, Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import chat as chat_api
from app.api.chat import (
    _admitted_controlled_stream,
    _control_internal,
    _ControlledResponseOwner,
    _queued_controlled_stream,
    _single_event_stream,
    _sse_response,
)
from app.api.contracts import DoneEvent, ErrorEvent, PingEvent
from app.config import Settings
from app.endpoint_controls import (
    CONTROL_MAX_INTEGER,
    CONTROL_MAX_RENEWAL_TICKS_PER_LEASE,
    CONTROL_QUEUE_POLL_INTERVAL_SECONDS,
    AdmissionRequest,
    Admitted,
    BudgetReconciliation,
    CancelledTicket,
    ClientIdentityError,
    ClientPeerError,
    ControlConfigurationError,
    ControlLease,
    ControlOperationReceipt,
    ControlStoreError,
    ControlTicket,
    Denied,
    EndpointControlConfig,
    EndpointController,
    EndpointControllerError,
    Finalized,
    InMemoryEndpointControlStore,
    Lost,
    Pending,
    ProductionEndpointControlStore,
    Queued,
    Renewed,
    call_mutation_with_one_replay,
    canonical_utc_instant,
    control_operation_receipt_from_json,
    control_operation_receipt_to_json,
    copy_control_operation_receipt,
    decode_digest_key,
    endpoint_control_config_from_settings,
    opaque_identity_digest,
    reconciliation_from_summary,
    resolve_client_identity,
)
from app.main import create_app
from app.providers.accounting import ProviderAttemptCostRecord
from app.providers.contracts import ProviderUsage
from app.request_accounting import AttemptIdentity, RequestAccountingSummary, SettledAttempt


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def config(**changes: object) -> EndpointControlConfig:
    values: dict[str, object] = {
        "trusted_proxy_cidrs": (),
        "digest_key": b"k" * 32,
        "ip_capacity": 2,
        "ip_refill_per_minute": Decimal("60"),
        "session_capacity": 2,
        "session_refill_per_minute": Decimal("60"),
        "rate_state_ttl_seconds": 600,
        "max_keys": 500,
        "max_active_requests": 1,
        "max_queued_requests": 1,
        "queue_wait_seconds": Decimal("2"),
        "lease_ttl_seconds": Decimal("30"),
        "lease_renew_seconds": Decimal("10"),
        "budget_currency": "USD",
        "budget_hourly": Decimal("10"),
        "budget_daily": Decimal("20"),
        "budget_reserve_per_attempt": Decimal("1"),
        "maximum_provider_attempts": 1,
    }
    values.update(changes)
    return EndpointControlConfig(**cast(Any, values))


def controlled_settings(tmp_path: Path, **changes: object) -> Settings:
    key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    values: dict[str, object] = {
        "database_path": tmp_path / "test.db",
        "chroma_path": tmp_path / "chroma",
        "provider": "echo",
        "embedding_provider": "fake",
        "public_endpoint_controls_enabled": True,
        "trusted_proxy_cidrs": "",
        "public_control_digest_key": SecretStr(key),
        "public_rate_ip_capacity": 2,
        "public_rate_ip_refill_per_minute": "60",
        "public_rate_session_capacity": 2,
        "public_rate_session_refill_per_minute": "60",
        "public_rate_state_ttl_seconds": 600,
        "public_control_max_keys": 500,
        "public_max_active_requests": 1,
        "public_max_queued_requests": 1,
        "public_queue_wait_seconds": "2",
        "public_lease_ttl_seconds": "30",
        "public_lease_renew_seconds": "10",
        "public_budget_currency": "USD",
        "public_budget_hourly": "10",
        "public_budget_daily": "20",
        "public_budget_reserve_per_attempt": "1",
    }
    values.update(changes)
    return Settings(**cast(Any, values))


def request(name: str, *, attempts: int = 1) -> AdmissionRequest:
    return AdmissionRequest(
        ip_digest=hashlib.sha256(f"ip:{name}".encode()).hexdigest(),
        session_digest=hashlib.sha256(f"session:{name}".encode()).hexdigest(),
        max_provider_attempts=attempts,
    )


def test_identity_ignores_untrusted_forwarding_and_walks_trusted_chain() -> None:
    assert (
        resolve_client_identity("203.0.113.9", [(b"x-forwarded-for", b"198.51.100.8")], ())
        == "203.0.113.9"
    )
    networks = ("10.0.0.0/8", "2001:db8:ffff::/48")
    assert (
        resolve_client_identity(
            "10.0.0.7",
            [(b"x-forwarded-for", b"198.51.100.4, 10.2.3.4")],
            networks,
        )
        == "198.51.100.4"
    )


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"unknown",
        b"example.test",
        b"198.51.100.1:443",
        b"[2001:db8::1]",
        b"fe80::1%eth0",
        b"198.51.100.1,,10.0.0.1",
        b"\xff",
    ],
)
def test_trusted_forwarding_rejects_malformed_values_content_free(header: bytes) -> None:
    with pytest.raises(ClientIdentityError) as caught:
        resolve_client_identity("10.0.0.7", [(b"x-forwarded-for", header)], ("10.0.0.0/8",))
    assert str(caught.value) == "ClientIdentityError()"
    assert repr(caught.value) == "ClientIdentityError()"
    assert caught.value.args == ()


def test_duplicate_forwarding_is_fatal_only_for_trusted_peer() -> None:
    headers = [(b"x-forwarded-for", b"198.51.100.1"), (b"X-Forwarded-For", b"198.51.100.2")]
    assert resolve_client_identity("203.0.113.1", headers, ("10.0.0.0/8",)) == "203.0.113.1"
    with pytest.raises(ClientIdentityError):
        resolve_client_identity("10.0.0.1", headers, ("10.0.0.0/8",))


def test_mapped_ipv6_is_canonical_ipv4_and_missing_peer_fails() -> None:
    assert resolve_client_identity("::ffff:192.0.2.9", [], ()) == "192.0.2.9"
    with pytest.raises(ClientIdentityError):
        resolve_client_identity(None, [], ())


def test_digest_key_and_domain_framing_are_exact() -> None:
    raw = bytes(range(32))
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert decode_digest_key(encoded) == raw
    expected = hmac.new(raw, b"cairn-control-v1\x00ip\x00192.0.2.1", hashlib.sha256).hexdigest()
    assert opaque_identity_digest(raw, "ip", "192.0.2.1") == expected
    assert opaque_identity_digest(raw, "ip", "same") != opaque_identity_digest(
        raw, "session", "same"
    )
    for invalid in (
        "",
        " " + encoded,
        encoded + "=",
        base64.urlsafe_b64encode(b"short").decode().rstrip("="),
    ):
        with pytest.raises(ControlConfigurationError):
            decode_digest_key(invalid)


def test_configuration_capacity_formula_and_renewal_bounds() -> None:
    cfg = config(max_keys=500)
    assert cfg.poll_attempts == 2
    assert cfg.structural_reserve == 12
    assert cfg.minimum_receipts == 67
    assert cfg.minimum_keys == 79
    assert cfg.receipt_capacity == 488
    assert CONTROL_QUEUE_POLL_INTERVAL_SECONDS == 1.0
    assert CONTROL_MAX_RENEWAL_TICKS_PER_LEASE == 60
    with pytest.raises(ControlConfigurationError):
        config(max_keys=78)
    with pytest.raises(ControlConfigurationError):
        config(lease_renew_seconds=Decimal("9.999"))
    with pytest.raises(ControlConfigurationError):
        config(lease_ttl_seconds=Decimal("30"), lease_renew_seconds=Decimal("10.001"))


def test_canonical_utc_instant_is_exact() -> None:
    assert (
        canonical_utc_instant(datetime(2026, 9, 11, 12, 3, 4, 5, tzinfo=UTC))
        == "2026-09-11T12:03:04.000005Z"
    )
    with pytest.raises(ControlStoreError):
        canonical_utc_instant(datetime(2026, 9, 11, 12, 3, 4))


def test_settings_policy_is_post_snapshot_and_production_fails_closed(tmp_path: Path) -> None:
    settings = controlled_settings(tmp_path)
    parsed = endpoint_control_config_from_settings(settings)
    assert parsed is not None
    assert parsed.maximum_provider_attempts == 0
    assert endpoint_control_config_from_settings(Settings()) is None
    with pytest.raises(ControlConfigurationError):
        endpoint_control_config_from_settings(
            Settings(
                deployment_mode="production",
                provider="ollama",
                embedding_provider="ollama",
            )
        )


def test_production_requires_nominal_non_memory_shared_store(tmp_path: Path) -> None:
    production = controlled_settings(
        tmp_path,
        deployment_mode="production",
        provider="ollama",
        embedding_provider="ollama",
    )
    parsed = endpoint_control_config_from_settings(production)
    assert parsed is not None

    class MemorySubclass(InMemoryEndpointControlStore, ProductionEndpointControlStore):
        pass

    with pytest.raises(ControlConfigurationError):
        create_app(production, endpoint_control_store=MemorySubclass(parsed, clock=Clock()))

    class ProductionStore(ProductionEndpointControlStore):
        async def admit_or_enqueue(self, *args: object) -> Denied:
            del args
            return Denied("concurrency_limited")

        async def wait_for_admission(self, *args: object) -> Denied:
            del args
            return Denied("concurrency_limited")

        async def renew(self, *args: object) -> Lost:
            del args
            return Lost("fencing_lost")

        async def finalize(self, *args: object) -> Finalized:
            del args
            return Finalized()

        async def cancel_ticket(self, *args: object) -> CancelledTicket:
            del args
            return CancelledTicket()

        async def check_budget_readiness(self) -> object:
            return object()

    application = create_app(production, endpoint_control_store=cast(Any, ProductionStore()))
    assert isinstance(application.state.endpoint_controller, EndpointController)


def test_injected_control_store_lifetime_is_always_borrowed(tmp_path: Path) -> None:
    settings = controlled_settings(tmp_path)
    parsed = endpoint_control_config_from_settings(settings)
    assert parsed is not None

    class BorrowedStore(InMemoryEndpointControlStore):
        def __init__(self) -> None:
            super().__init__(cast(EndpointControlConfig, parsed), clock=Clock())
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        async def aclose(self) -> None:
            self.close_calls += 1

        def __enter__(self) -> BorrowedStore:
            self.close_calls += 1
            return self

        def __exit__(self, *args: object) -> None:
            del args
            self.close_calls += 1

    store = BorrowedStore()
    application = create_app(settings, endpoint_control_store=store)
    with TestClient(application, client=("192.0.2.1", 50000)) as client:
        response = client.post(
            "/api/v1/chat/message",
            json={"session_id": "s", "message": "hello"},
        )
        assert response.status_code == 200
    assert store.close_calls == 0


def test_controlled_app_refusal_reconciles_lexical_lease(tmp_path: Path) -> None:
    settings = controlled_settings(tmp_path)
    parsed = endpoint_control_config_from_settings(settings)
    assert parsed is not None
    store = InMemoryEndpointControlStore(parsed, clock=Clock())
    app = create_app(settings, endpoint_control_store=store)
    with TestClient(app, client=("192.0.2.1", 50000)) as client:
        response = client.post(
            "/api/v1/chat/message",
            json={"session_id": "s", "message": "hello"},
        )
    assert response.status_code == 200
    assert response.text.startswith("event: status\n")
    assert '"finish_reason":"refused"' in response.text
    assert store.snapshot()["counts"]["leases"] == 0


def test_trusted_proxy_invalid_header_is_fixed_public_denial(tmp_path: Path) -> None:
    settings = controlled_settings(tmp_path, trusted_proxy_cidrs="10.0.0.0/8")
    parsed = endpoint_control_config_from_settings(settings)
    assert parsed is not None
    store = InMemoryEndpointControlStore(parsed, clock=Clock())
    app = create_app(settings, endpoint_control_store=store)
    with TestClient(app, client=("10.0.0.1", 50000)) as client:
        response = client.post(
            "/api/v1/chat/message",
            headers={"x-forwarded-for": "not-an-ip"},
            json={"session_id": "s", "message": "hello"},
        )
    assert response.text == (
        'event: error\ndata: {"type":"error","code":"invalid_request",'
        '"message":"The request could not be processed.","retryable":false}\n\n'
    )
    assert store.snapshot()["counts"]["receipts"] == 0


@pytest.mark.asyncio
async def test_atomic_rate_product_and_idempotent_replay() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(), clock=clock)
    first = await store.admit_or_enqueue("0" * 32, request("one"))
    assert isinstance(first, Admitted)
    replay = await store.admit_or_enqueue("0" * 32, request("one"))
    assert replay == first
    with pytest.raises(ControlStoreError):
        await store.admit_or_enqueue("0" * 32, request("different"))
    await store.finalize(
        "1" * 32,
        first.lease.lease_id,
        first.lease.fencing_token,
        BudgetReconciliation(charge=Decimal("0"), currency="USD", accounting_uncertain=False),
    )
    second = await store.admit_or_enqueue("2" * 32, request("one"))
    assert isinstance(second, Admitted)
    await store.finalize(
        "3" * 32,
        second.lease.lease_id,
        second.lease.fencing_token,
        BudgetReconciliation(charge=Decimal("0"), currency="USD", accounting_uncertain=False),
    )
    denied = await store.admit_or_enqueue("4" * 32, request("one"))
    assert denied == Denied(reason="ip_rate_limited")


@pytest.mark.asyncio
async def test_two_instances_share_active_limit_and_fifo_queue() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    first = await store.admit_or_enqueue("0" * 32, request("a"))
    second = await store.admit_or_enqueue("1" * 32, request("b"))
    third = await store.admit_or_enqueue("2" * 32, request("c"))
    assert isinstance(first, Admitted)
    assert isinstance(second, Queued)
    assert third == Denied(reason="concurrency_limited")
    assert isinstance(
        await store.wait_for_admission(
            "3" * 32, second.ticket.ticket_id, second.ticket.fencing_token
        ),
        Pending,
    )
    await store.finalize(
        "4" * 32,
        first.lease.lease_id,
        first.lease.fencing_token,
        BudgetReconciliation(charge=Decimal("0"), currency="USD", accounting_uncertain=False),
    )
    promoted = await store.wait_for_admission(
        "5" * 32, second.ticket.ticket_id, second.ticket.fencing_token
    )
    assert isinstance(promoted, Admitted)


@pytest.mark.asyncio
async def test_budget_reservation_reconciliation_and_original_windows() -> None:
    clock = Clock()
    clock.now = datetime(2026, 9, 11, 12, 59, 50, tzinfo=UTC)
    store = InMemoryEndpointControlStore(
        config(ip_capacity=10, session_capacity=10, maximum_provider_attempts=2), clock=clock
    )
    admitted = await store.admit_or_enqueue("0" * 32, request("a", attempts=2))
    assert isinstance(admitted, Admitted)
    assert admitted.lease.reserved_amount == Decimal("2")
    assert admitted.lease.reservation_instant == "2026-09-11T12:59:50.000000Z"
    assert admitted.lease.hour_window == "2026-09-11T12:00:00Z"
    assert admitted.lease.day_window == "2026-09-11"
    clock.advance(20)
    await store.finalize(
        "1" * 32,
        admitted.lease.lease_id,
        admitted.lease.fencing_token,
        BudgetReconciliation(charge=Decimal("0.25"), currency="USD", accounting_uncertain=False),
    )
    snapshot = store.snapshot()
    assert snapshot["budget"]["2026-09-11T12:00:00Z"]["committed"] == Decimal("0.25")
    assert snapshot["budget"]["2026-09-11T12:00:00Z"]["reserved"] == Decimal("0")


@pytest.mark.asyncio
async def test_renewal_and_stale_fence_fail_closed() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    admitted = await store.admit_or_enqueue("0" * 32, request("a"))
    assert isinstance(admitted, Admitted)
    clock.advance(10)
    renewed = await store.renew("1" * 32, admitted.lease.lease_id, admitted.lease.fencing_token)
    assert isinstance(renewed, Renewed)
    with pytest.raises(ControlStoreError):
        await store.finalize(
            "2" * 32,
            admitted.lease.lease_id,
            "f" * 32,
            BudgetReconciliation(charge=Decimal("0"), currency="USD", accounting_uncertain=False),
        )


@pytest.mark.asyncio
async def test_clock_rollback_and_expired_lease_are_conservative() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    admitted = await store.admit_or_enqueue("0" * 32, request("a"))
    assert isinstance(admitted, Admitted)
    clock.advance(31)
    assert isinstance(await store.admit_or_enqueue("1" * 32, request("b")), Admitted)
    clock.advance(-40)
    with pytest.raises(ControlStoreError):
        await store.check_budget_readiness()


@pytest.mark.asyncio
async def test_concurrent_same_operation_has_one_mutation() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    outcomes = await asyncio.gather(
        store.admit_or_enqueue("0" * 32, request("a")),
        store.admit_or_enqueue("0" * 32, request("a")),
    )
    assert outcomes[0] == outcomes[1]
    assert store.snapshot()["counts"]["leases"] == 1


@pytest.mark.parametrize(
    ("currency", "uncertain"),
    [("usd", False), ("US\x00", False), ("US1", False), ("USD", 1)],
)
def test_reconciliation_rejects_noncanonical_currency_and_bool(
    currency: str, uncertain: object
) -> None:
    with pytest.raises(ControlStoreError):
        BudgetReconciliation(
            charge=Decimal("1"),
            currency=currency,
            accounting_uncertain=cast(Any, uncertain),
        )


def test_decimal_positive_exponent_is_bounded_before_formatting() -> None:
    with pytest.raises(ControlStoreError):
        BudgetReconciliation(
            charge=Decimal("1E+1000000"),
            currency="USD",
            accounting_uncertain=False,
        )


def _lease() -> ControlLease:
    return ControlLease(
        lease_id="1" * 32,
        fencing_token="2" * 32,
        expires_at="2026-09-11T12:00:30.000000Z",
        reservation_instant="2026-09-11T12:00:00.000000Z",
        attempt_date=datetime(2026, 9, 11, tzinfo=UTC).date(),
        hour_window="2026-09-11T12:00:00Z",
        day_window="2026-09-11",
        reserved_amount=Decimal("2"),
        currency="USD",
    )


class _MatrixStore:
    def __init__(
        self,
        selected: str,
        ticket: ControlTicket,
        *,
        errors: tuple[str, ...] = (),
        block: bool = False,
        commit_before_error: bool = False,
    ) -> None:
        self.selected = selected
        self.ticket = ticket
        self.errors = list(errors)
        self.block = block
        self.commit_before_error = commit_before_error
        self.commits = 0
        self.calls: list[tuple[str, str | None, tuple[object, ...]]] = []
        self.started = asyncio.Event()
        self.blocker = asyncio.Event()

    async def _before(
        self, kind: str, operation_id: str | None, payload: tuple[object, ...]
    ) -> None:
        self.calls.append((kind, operation_id, payload))
        if kind != self.selected:
            return
        self.started.set()
        if self.block:
            await self.blocker.wait()
        if self.errors:
            code = self.errors.pop(0)
            if self.commit_before_error:
                self.commits += 1
            raise ControlStoreError(cast(Any, code))

    async def admit_or_enqueue(
        self, operation_id: str, admission: AdmissionRequest
    ) -> Denied:
        await self._before("admit_or_enqueue", operation_id, (admission,))
        return Denied("concurrency_limited")

    async def wait_for_admission(
        self, operation_id: str, ticket_id: str, fencing_token: str
    ) -> Pending:
        await self._before(
            "wait_for_admission", operation_id, (ticket_id, fencing_token)
        )
        return Pending(self.ticket)

    async def renew(
        self, operation_id: str, lease_id: str, fencing_token: str
    ) -> Lost:
        await self._before("renew", operation_id, (lease_id, fencing_token))
        return Lost("fencing_lost")

    async def finalize(
        self,
        operation_id: str,
        lease_id: str,
        fencing_token: str,
        reconciliation: BudgetReconciliation,
    ) -> Finalized:
        await self._before(
            "finalize",
            operation_id,
            (lease_id, fencing_token, reconciliation),
        )
        return Finalized()

    async def cancel_ticket(
        self, operation_id: str, ticket_id: str, fencing_token: str
    ) -> CancelledTicket:
        await self._before(
            "cancel_ticket", operation_id, (ticket_id, fencing_token)
        )
        return CancelledTicket()

    async def check_budget_readiness(self) -> object:
        await self._before("check_budget_readiness", None, ())
        return object()


def _seed_matrix_lease(controller: EndpointController, lease: ControlLease) -> None:
    key = (lease.lease_id, lease.fencing_token)
    cast(Any, controller)._lease_deadlines[key] = 100.0
    cast(Any, controller)._lease_store_expiries[key] = datetime(
        2026, 9, 11, 12, 0, 30, tzinfo=UTC
    )


async def _invoke_matrix_operation(
    controller: EndpointController,
    kind: str,
    ticket: ControlTicket,
    lease: ControlLease,
) -> object:
    if kind == "admit_or_enqueue":
        return await controller.admit("192.0.2.1", (), "session", 1)
    if kind == "wait_for_admission":
        return await controller.poll(ticket)
    if kind == "renew":
        return await controller.renew(lease)
    if kind == "finalize":
        await controller.finalize_without_provider(lease)
        return None
    if kind == "cancel_ticket":
        await controller.cancel(ticket)
        return None
    return await controller.check_readiness()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ["lease", "ticket"])
@pytest.mark.parametrize("failure_phase", ["response_start", "first_body"])
async def test_control_resource_is_reconciled_when_stream_send_fails(
    resource_kind: str, failure_phase: str
) -> None:
    class Controller:
        def __init__(self) -> None:
            self.finalized: list[ControlLease] = []
            self.cancelled: list[ControlTicket] = []

        async def finalize_without_provider(self, lease: ControlLease) -> None:
            self.finalized.append(lease)

        async def cancel(self, ticket: ControlTicket) -> None:
            self.cancelled.append(ticket)

    controller = Controller()
    owner = _ControlledResponseOwner(cast(Any, controller))
    lease = _lease()
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:00:30.000000Z",
    )
    if resource_kind == "lease":
        owner.claim(lease)
    else:
        owner.claim_ticket(ticket)
    response = _sse_response(
        _single_event_stream(_control_internal()),
        controlled_owner=owner,
    )
    body_sends = 0

    async def send(message: dict[str, object]) -> None:
        nonlocal body_sends
        if message["type"] == "http.response.start" and failure_phase == "response_start":
            raise OSError("PRIVATE-START")
        if message["type"] == "http.response.body":
            body_sends += 1
            if failure_phase == "first_body" and body_sends == 1:
                raise OSError("PRIVATE-BODY")

    with pytest.raises(OSError):
        await response.stream_response(cast(Any, send))
    assert controller.finalized == ([lease] if resource_kind == "lease" else [])
    assert controller.cancelled == ([ticket] if resource_kind == "ticket" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [0.0, -0.000001, "failure"])
async def test_expired_or_invalid_local_authority_starts_no_protected_work(
    remaining: float | str,
) -> None:
    class Controller:
        lease_renew_seconds = 10.0

        def __init__(self) -> None:
            self.authority_calls = 0
            self.sleep_calls = 0

        def authority_remaining(self, lease: ControlLease) -> float:
            del lease
            self.authority_calls += 1
            if remaining == "failure":
                raise EndpointControllerError from None
            return cast(float, remaining)

        async def sleep(self, delay: float) -> None:
            del delay
            self.sleep_calls += 1

    class Factory:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **kwargs: object) -> object:
            del kwargs
            self.calls += 1
            return object()

    controller = Controller()
    factory = Factory()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=object(),
            provider=object(),
            provider_accounting_binding=object(),
            request_accounting_factory=factory,
        )
    )
    owner = _ControlledResponseOwner(cast(Any, controller))
    events = [
        event
        async for event in _admitted_controlled_stream(
            request=cast(Any, SimpleNamespace(app=app)),
            body=cast(Any, object()),
            controller=cast(Any, controller),
            lease=_lease(),
            response_owner=owner,
        )
    ]
    assert events == [_control_internal()]
    assert controller.authority_calls == 1
    assert controller.sleep_calls == 0
    assert factory.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining_at_delivery", [0.000001, 0.0, -0.000001])
async def test_provider_event_is_never_delivered_at_or_after_local_authority(
    monkeypatch: pytest.MonkeyPatch,
    remaining_at_delivery: float,
) -> None:
    class Controller:
        lease_renew_seconds = 10.0

        def __init__(self) -> None:
            self.readings = iter((30.0, 30.0, 30.0, remaining_at_delivery))

        def authority_remaining(self, lease: ControlLease) -> float:
            del lease
            return next(self.readings)

        async def sleep(self, delay: float) -> None:
            del delay
            await asyncio.Event().wait()

    class Factory:
        def create(self, **kwargs: object) -> object:
            del kwargs
            return object()

    provider_starts = 0

    async def event_stream(*args: object, **kwargs: object) -> Any:
        nonlocal provider_starts
        del args, kwargs
        provider_starts += 1
        yield DoneEvent(finish_reason="stop")

    monkeypatch.setattr(chat_api, "validate_controlled_provider_binding", lambda *args: None)
    monkeypatch.setattr(chat_api, "controlled_provider_observer", lambda *args: object())
    monkeypatch.setattr(chat_api, "chat_event_stream", event_stream)
    controller = Controller()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                retrieval_top_k=4,
                system_instruction="",
                max_output_tokens=16,
                max_output_chars=256,
            ),
            provider=object(),
            provider_accounting_binding=object(),
            request_accounting_factory=Factory(),
            retrieval_route_resolver=object(),
            retrieval_max_distance=1.0,
            retrieval_distance_measure="cosine",
        )
    )
    owner = _ControlledResponseOwner(cast(Any, controller))
    events = [
        event
        async for event in _admitted_controlled_stream(
            request=cast(Any, SimpleNamespace(app=app)),
            body=cast(Any, object()),
            controller=cast(Any, controller),
            lease=_lease(),
            response_owner=owner,
        )
    ]
    assert provider_starts == 1
    if remaining_at_delivery > 0:
        assert events == [DoneEvent(finish_reason="stop")]
    else:
        assert events == [_control_internal()]


@pytest.mark.asyncio
async def test_terminal_provider_event_drains_done_failed_renewal_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewal_failure = asyncio.Event()

    class Controller:
        lease_renew_seconds = 10.0

        def authority_remaining(self, lease: ControlLease) -> float:
            del lease
            return 30.0

        async def sleep(self, delay: float) -> None:
            del delay
            await renewal_failure.wait()
            raise EndpointControllerError from None

    class Factory:
        def create(self, **kwargs: object) -> object:
            del kwargs
            return object()

    async def event_stream(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        yield DoneEvent(finish_reason="stop")

    monkeypatch.setattr(chat_api, "validate_controlled_provider_binding", lambda *args: None)
    monkeypatch.setattr(chat_api, "controlled_provider_observer", lambda *args: object())
    monkeypatch.setattr(chat_api, "chat_event_stream", event_stream)
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                retrieval_top_k=4,
                system_instruction="",
                max_output_tokens=16,
                max_output_chars=256,
            ),
            provider=object(),
            provider_accounting_binding=object(),
            request_accounting_factory=Factory(),
            retrieval_route_resolver=object(),
            retrieval_max_distance=1.0,
            retrieval_distance_measure="cosine",
        )
    )
    controller = Controller()
    owner = _ControlledResponseOwner(cast(Any, controller))
    stream = _admitted_controlled_stream(
        request=cast(Any, SimpleNamespace(app=app)),
        body=cast(Any, object()),
        controller=cast(Any, controller),
        lease=_lease(),
        response_owner=owner,
    )
    loop = asyncio.get_running_loop()
    prior_handler = loop.get_exception_handler()
    unhandled: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        assert await anext(stream) == DoneEvent(finish_reason="stop")
        renewal_failure.set()
        await asyncio.sleep(0)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(prior_handler)
    assert unhandled == []


@pytest.mark.asyncio
async def test_stream_stops_and_finalizes_before_would_be_renewal_tick_61(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def active_cleanup_uncertain(self) -> None:
            return None

        def finalize(self, completion: object) -> RequestAccountingSummary:
            return RequestAccountingSummary(
                provider_work_started=False,
                request_completion=cast(Any, completion),
                attempts=(),
            )

    class Factory:
        def create(self, **kwargs: object) -> Session:
            del kwargs
            return Session()

    class Controller:
        lease_renew_seconds = 10.0

        def __init__(self) -> None:
            self.sleepers: list[asyncio.Event] = []
            self.renew_calls = 0
            self.finalize_calls = 0

        def authority_remaining(self, lease: ControlLease) -> float:
            del lease
            return 30.0

        async def sleep(self, delay: float) -> None:
            assert delay == 10.0
            event = asyncio.Event()
            self.sleepers.append(event)
            await event.wait()

        async def renew(self, lease: ControlLease) -> Renewed:
            del lease
            self.renew_calls += 1
            return Renewed("2026-09-11T12:00:40.000000Z")

        async def finalize(
            self, lease: ControlLease, summary: RequestAccountingSummary
        ) -> None:
            del lease, summary
            self.finalize_calls += 1

    source_closed = 0

    async def event_stream(*args: object, **kwargs: object) -> Any:
        nonlocal source_closed
        del args, kwargs
        try:
            await asyncio.Event().wait()
        finally:
            source_closed += 1
        if False:
            yield DoneEvent(finish_reason="stop")

    monkeypatch.setattr(chat_api, "validate_controlled_provider_binding", lambda *args: None)
    monkeypatch.setattr(chat_api, "controlled_provider_observer", lambda *args: object())
    monkeypatch.setattr(chat_api, "chat_event_stream", event_stream)
    controller = Controller()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                retrieval_top_k=4,
                system_instruction="",
                max_output_tokens=16,
                max_output_chars=256,
            ),
            provider=object(),
            provider_accounting_binding=object(),
            request_accounting_factory=Factory(),
            retrieval_route_resolver=object(),
            retrieval_max_distance=1.0,
            retrieval_distance_measure="cosine",
        )
    )
    owner = _ControlledResponseOwner(cast(Any, controller))
    lease = _lease()
    owner.claim(lease)
    response = _sse_response(
        _admitted_controlled_stream(
            request=cast(Any, SimpleNamespace(app=app)),
            body=cast(Any, object()),
            controller=cast(Any, controller),
            lease=lease,
            response_owner=owner,
        ),
        controlled_owner=owner,
    )
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    response_task = asyncio.create_task(response.stream_response(cast(Any, send)))

    async def wait_for_sleeper(index: int) -> None:
        for _ in range(100):
            if len(controller.sleepers) > index:
                return
            await asyncio.sleep(0)
        raise AssertionError("renewal sleeper was not scheduled")

    for index in range(61):
        await wait_for_sleeper(index)
        controller.sleepers[index].set()
        await asyncio.sleep(0)
    await asyncio.wait_for(response_task, timeout=1)

    assert controller.renew_calls == 60
    assert controller.finalize_calls == 1
    assert source_closed == 1
    bodies = [
        cast(bytes, message.get("body", b""))
        for message in sent
        if message["type"] == "http.response.body"
    ]
    assert b'"code":"internal"' in b"".join(bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wait_seconds",
    [0.0, 0.999999, 1.0, 1.000001, 15.0, 15.000001, 16.0],
)
async def test_queue_uses_exact_absolute_poll_and_ping_schedule(wait_seconds: float) -> None:
    class Scheduler:
        def __init__(self) -> None:
            self.now = 0.0
            self.waiters: list[tuple[float, asyncio.Event]] = []

        async def sleep(self, delay: float) -> None:
            event = asyncio.Event()
            target = self.now + delay
            self.waiters.append((target, event))
            if target <= self.now:
                event.set()
            await event.wait()

        async def advance(self, instant: float) -> None:
            self.now = instant
            for target, event in self.waiters:
                if target <= instant:
                    event.set()
            for _ in range(8):
                await asyncio.sleep(0)

    class Controller:
        def __init__(self, scheduler: Scheduler) -> None:
            self.scheduler = scheduler
            self.queue_wait_seconds = wait_seconds
            self.poll_times: list[float] = []

        def monotonic(self) -> float:
            return self.scheduler.now

        def deadline_from(self, start: float, duration: float) -> float:
            return start + duration

        async def sleep(self, delay: float) -> None:
            await self.scheduler.sleep(delay)

        async def poll(
            self, ticket: ControlTicket, *, overall_deadline: float
        ) -> Pending:
            assert self.scheduler.now < overall_deadline
            self.poll_times.append(self.scheduler.now)
            await asyncio.sleep(0)
            return Pending(ticket)

    scheduler = Scheduler()
    controller = Controller(scheduler)
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:05:10.000000Z",
    )
    queued = Queued(ticket)
    owner = _ControlledResponseOwner(cast(Any, controller))
    owner.claim_ticket(ticket)

    async def collect() -> list[object]:
        return [
            event
            async for event in _queued_controlled_stream(
                request=cast(Any, object()),
                body=cast(Any, object()),
                controller=cast(Any, controller),
                queued=queued,
                response_owner=owner,
            )
        ]

    task = asyncio.create_task(collect())
    for _ in range(8):
        await asyncio.sleep(0)
    for instant in range(1, math.ceil(wait_seconds)):
        await scheduler.advance(float(instant))
    await scheduler.advance(wait_seconds)
    events = await asyncio.wait_for(task, timeout=1)

    assert controller.poll_times == [float(value) for value in range(math.ceil(wait_seconds))]
    expected_pings = [
        PingEvent() for instant in range(15, 301, 15) if instant < wait_seconds
    ]
    assert events[:-1] == expected_pings
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "concurrency_limited"


@pytest.mark.asyncio
async def test_five_second_queue_polls_do_not_suppress_absolute_ping() -> None:
    class Scheduler:
        def __init__(self) -> None:
            self.now = 0.0
            self.waiters: list[tuple[float, asyncio.Event]] = []

        async def sleep(self, delay: float) -> None:
            event = asyncio.Event()
            self.waiters.append((self.now + delay, event))
            await event.wait()

        async def advance(self, instant: float) -> None:
            self.now = instant
            for target, event in self.waiters:
                if target <= instant:
                    event.set()
            for _ in range(12):
                await asyncio.sleep(0)

    class Controller:
        queue_wait_seconds = 16.0

        def __init__(self, scheduler: Scheduler) -> None:
            self.scheduler = scheduler
            self.poll_times: list[float] = []

        def monotonic(self) -> float:
            return self.scheduler.now

        def deadline_from(self, start: float, duration: float) -> float:
            return start + duration

        async def sleep(self, delay: float) -> None:
            await self.scheduler.sleep(delay)

        async def poll(
            self, ticket: ControlTicket, *, overall_deadline: float
        ) -> Pending:
            assert self.scheduler.now < overall_deadline
            self.poll_times.append(self.scheduler.now)
            await self.scheduler.sleep(5.0)
            return Pending(ticket)

    scheduler = Scheduler()
    controller = Controller(scheduler)
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:05:10.000000Z",
    )
    owner = _ControlledResponseOwner(cast(Any, controller))
    owner.claim_ticket(ticket)

    async def collect() -> list[object]:
        return [
            event
            async for event in _queued_controlled_stream(
                request=cast(Any, object()),
                body=cast(Any, object()),
                controller=cast(Any, controller),
                queued=Queued(ticket),
                response_owner=owner,
            )
        ]

    task = asyncio.create_task(collect())
    for _ in range(12):
        await asyncio.sleep(0)
    for instant in (5.0, 10.0, 15.0, 16.0):
        await scheduler.advance(instant)
    events = await asyncio.wait_for(task, timeout=1)
    assert controller.poll_times == [0.0, 5.0, 10.0, 15.0]
    assert len(events) == 2
    assert isinstance(events[0], PingEvent)
    assert isinstance(events[1], ErrorEvent)
    assert events[1].code == "concurrency_limited"


@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        (Admitted(_lease()), "admit_or_enqueue"),
        (
            Queued(ControlTicket("1" * 32, "2" * 32, "2026-09-11T12:00:30.000000Z")),
            "admit_or_enqueue",
        ),
        (
            Pending(ControlTicket("1" * 32, "2" * 32, "2026-09-11T12:00:30.000000Z")),
            "wait_for_admission",
        ),
        (Denied("budget_exhausted"), "admit_or_enqueue"),
        (Renewed("2026-09-11T12:00:30.000000Z"), "renew"),
        (Lost("lease_expired"), "renew"),
        (Finalized(), "finalize"),
        (CancelledTicket(), "cancel_ticket"),
    ],
)
def test_receipt_rejects_bypass_mutated_outcome_discriminator(outcome: object, kind: str) -> None:
    object.__setattr__(outcome, "outcome", "PRIVATE-CANARY")
    with pytest.raises(ControlStoreError):
        ControlOperationReceipt(
            operation_id="a" * 32,
            kind=cast(Any, kind),
            request_fingerprint="b" * 64,
            outcome=cast(Any, outcome),
            committed_at="2026-09-11T12:00:00.000000Z",
            expires_at="2026-09-11T12:10:00.000000Z",
        )


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("admit_or_enqueue", Renewed("2026-09-11T12:00:30.000000Z")),
        ("wait_for_admission", Denied("ip_rate_limited")),
        ("renew", Finalized()),
        ("finalize", CancelledTicket()),
        ("cancel_ticket", Admitted(_lease())),
    ],
)
def test_receipt_rejects_cross_kind_outcome_swaps(kind: str, outcome: object) -> None:
    with pytest.raises(ControlStoreError):
        _receipt(kind, outcome)


def test_control_error_ordinary_surfaces_hide_raw_context() -> None:
    clock = Clock()

    def failing_clock() -> datetime:
        raise RuntimeError("PRIVATE-CLOCK")

    store = InMemoryEndpointControlStore(config(), clock=failing_clock)
    with pytest.raises(ControlStoreError) as caught:
        asyncio.run(store.check_budget_readiness())
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert caught.value.__traceback__ is None
    assert "PRIVATE" not in str(caught.value)
    assert "PRIVATE" not in caught.value.json()
    del clock


@pytest.mark.asyncio
async def test_receipt_expiry_never_replays_an_expired_lease() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(
        config(ip_capacity=10, session_capacity=10),
        clock=clock,
        token_factory=iter(("1" * 32, "2" * 32, "1" * 32, "2" * 32)).__next__,
    )
    first = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(first, Admitted)
    clock.advance(600)
    second = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(second, Admitted)
    assert (second.lease.lease_id, second.lease.fencing_token) != (
        first.lease.lease_id,
        first.lease.fencing_token,
    )


@pytest.mark.asyncio
async def test_controller_derives_fresh_logical_ids_from_repeated_factory_values() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    controller = EndpointController(
        store, config(ip_capacity=10, session_capacity=10), operation_id_factory=lambda: "a" * 32
    )
    admitted = await controller.admit("192.0.2.1", (), "s", 1)
    assert isinstance(admitted, Admitted)
    clock.advance(10)
    assert isinstance(await controller.renew(admitted.lease), Renewed)
    assert len(cast(Any, store)._receipts) == 2


def test_config_repr_never_exposes_digest_key() -> None:
    cfg = config(digest_key=bytes(range(32)))
    assert repr(cfg) == "EndpointControlConfig()"
    assert "\\x" not in repr(cfg)


def _receipt(kind: str, outcome: object) -> ControlOperationReceipt:
    return ControlOperationReceipt(
        operation_id="a" * 32,
        kind=cast(Any, kind),
        request_fingerprint="b" * 64,
        outcome=cast(Any, outcome),
        committed_at="2026-09-11T12:00:00.000000Z",
        expires_at="2026-09-11T12:10:00.000000Z",
    )


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("admit_or_enqueue", Admitted(_lease())),
        ("admit_or_enqueue", Denied("ip_rate_limited")),
        ("admit_or_enqueue", Denied("session_rate_limited")),
        ("admit_or_enqueue", Denied("budget_exhausted")),
        ("admit_or_enqueue", Denied("concurrency_limited")),
        (
            "admit_or_enqueue",
            Queued(ControlTicket("1" * 32, "2" * 32, "2026-09-11T12:00:30.000000Z")),
        ),
        (
            "wait_for_admission",
            Pending(ControlTicket("1" * 32, "2" * 32, "2026-09-11T12:00:30.000000Z")),
        ),
        ("wait_for_admission", Admitted(_lease())),
        ("wait_for_admission", Denied("budget_exhausted")),
        ("wait_for_admission", Denied("concurrency_limited")),
        ("renew", Renewed("2026-09-11T12:00:30.000000Z")),
        ("renew", Lost("fencing_lost")),
        ("renew", Lost("lease_expired")),
        ("finalize", Finalized()),
        ("cancel_ticket", CancelledTicket()),
    ],
)
def test_receipt_copy_and_json_adapter_round_trip(kind: str, outcome: object) -> None:
    receipt = _receipt(kind, outcome)
    copied = copy_control_operation_receipt(receipt)
    restored = control_operation_receipt_from_json(control_operation_receipt_to_json(receipt))
    assert copied == receipt
    assert copied is not receipt
    assert restored == receipt
    payload = json.loads(control_operation_receipt_to_json(receipt))
    payload["private"] = "CANARY"
    with pytest.raises(ControlStoreError):
        control_operation_receipt_from_json(json.dumps(payload))


def test_model_constructor_failure_releases_rejected_value_without_gc() -> None:
    class Canary:
        pass

    canary: object | None = Canary()
    reference = weakref.ref(canary)
    with pytest.raises(ControlStoreError) as caught:
        AdmissionRequest(cast(Any, canary), "a" * 64, 1)
    canary = None
    assert reference() is None
    assert caught.value.__traceback__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "adapter",
    [
        copy_control_operation_receipt,
        control_operation_receipt_to_json,
        control_operation_receipt_from_json,
    ],
)
def test_receipt_adapter_failure_releases_rejected_value_without_gc(
    adapter: Any,
) -> None:
    class Canary(str):
        pass

    value: object | None = Canary("PRIVATE-CANARY")
    reference = weakref.ref(value)
    with pytest.raises(ControlStoreError) as caught:
        adapter(value)
    value = None
    assert reference() is None
    assert caught.value.__traceback__ is None
    assert caught.value.__context__ is None


def test_identity_and_digest_failures_release_rejected_values_without_gc() -> None:
    class CanaryString(str):
        pass

    class CanaryHeaders(list[tuple[bytes, bytes]]):
        pass

    peer: object | None = CanaryString("PRIVATE-PEER")
    peer_reference = weakref.ref(peer)
    with pytest.raises(ClientPeerError):
        resolve_client_identity(peer, (), ())
    peer = None
    assert peer_reference() is None

    headers: object | None = CanaryHeaders([(b"x-forwarded-for", b"198.51.100.1")])
    headers_reference = weakref.ref(headers)
    with pytest.raises(ClientIdentityError):
        resolve_client_identity("10.0.0.1", cast(Any, headers), ("10.0.0.0/8",))
    headers = None
    assert headers_reference() is None

    key: object | None = CanaryString("PRIVATE-KEY")
    key_reference = weakref.ref(key)
    with pytest.raises(ControlConfigurationError):
        decode_digest_key(key)
    key = None
    assert key_reference() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "codes",
    [
        ("store_unavailable",),
        ("store_timeout", "store_timeout"),
        ("store_timeout", "store_conflict"),
    ],
)
async def test_replay_helper_failure_releases_operation_closure_without_gc(
    codes: tuple[str, ...],
) -> None:
    class Canary:
        pass

    async def capture() -> tuple[ControlStoreError, weakref.ReferenceType[object]]:
        canary: object | None = Canary()
        reference = weakref.ref(canary)
        remaining = iter(codes)

        async def operation(retained: object = canary) -> object:
            del retained
            raise ControlStoreError(cast(Any, next(remaining)))

        try:
            await call_mutation_with_one_replay(operation)
        except ControlStoreError as error:
            del operation, canary
            return error, reference
        raise AssertionError("fixed transport failure unexpectedly succeeded")

    error, reference = await capture()
    assert error.code == (
        "unknown_outcome" if codes == ("store_timeout", "store_timeout") else codes[-1]
    )
    assert reference() is None


@pytest.mark.asyncio
async def test_returned_outcome_never_aliases_internal_receipt() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    first = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(first, Admitted)
    original_expiry = first.lease.expires_at
    object.__setattr__(first.lease, "expires_at", "PRIVATE-CANARY")
    replay = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(replay, Admitted)
    assert replay.lease.expires_at == original_expiry


@pytest.mark.asyncio
async def test_receipt_collision_fails_before_expiry_pruning_mutates_state() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    assert isinstance(await store.admit_or_enqueue("a" * 32, request("a")), Admitted)
    before = store.snapshot()
    clock.advance(31)
    with pytest.raises(ControlStoreError):
        await store.admit_or_enqueue("a" * 32, request("different"))
    assert store.snapshot() == before


def test_decimal_arithmetic_ignores_ambient_context() -> None:
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)):
        cfg = config(
            budget_reserve_per_attempt=Decimal("1.23"),
            maximum_provider_attempts=2,
            budget_hourly=Decimal("10"),
        )
    assert cfg.budget_reserve_per_attempt == Decimal("1.23")

    usage = ProviderUsage(
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        thinking_tokens=0,
        total_tokens=2,
    )
    attempts: list[SettledAttempt] = []
    for number, completion, cost in ((1, "error", "1.23"), (2, "completed", "4.56")):
        identity = AttemptIdentity(provider="echo", model="echo-v1", provider_attempt=number)
        record = ProviderAttemptCostRecord(
            provider="echo",
            model="echo-v1",
            provider_attempt=number,
            attempt_date=datetime(2026, 9, 11, tzinfo=UTC).date(),
            completion_state=cast(Literal["completed", "error", "cancelled"], completion),
            answer_outcome="unverified",
            usage=usage,
            snapshot_id="a" * 64,
            currency="USD",
            model_cost=Decimal(cost),
            cost_state="priced",
        )
        attempts.append(SettledAttempt(identity=identity, cost_record=record))
    summary = RequestAccountingSummary(
        provider_work_started=True,
        request_completion="completed",
        attempts=tuple(attempts),
    )
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)):
        reconciliation = reconciliation_from_summary(
            summary, reserve_per_attempt=Decimal("9"), currency="USD"
        )
    assert reconciliation.charge == Decimal("5.79")
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)):
        with pytest.raises(ControlConfigurationError):
            config(
                lease_ttl_seconds=Decimal("30"),
                lease_renew_seconds=Decimal("10.01"),
            )


@pytest.mark.asyncio
async def test_overrun_blocks_subsequent_admission_until_window_expiry() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(config(ip_capacity=10, session_capacity=10), clock=clock)
    first = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(first, Admitted)
    await store.finalize(
        "b" * 32,
        first.lease.lease_id,
        first.lease.fencing_token,
        BudgetReconciliation(Decimal("2"), "USD", False),
    )
    assert await store.admit_or_enqueue("c" * 32, request("b")) == Denied("budget_exhausted")


@pytest.mark.asyncio
async def test_receipt_churn_cannot_consume_live_finalize_reserve() -> None:
    cfg = config(ip_capacity=1, session_capacity=1, max_keys=79)
    store = InMemoryEndpointControlStore(cfg, clock=Clock())
    admitted = await store.admit_or_enqueue("0" * 32, request("a"))
    assert isinstance(admitted, Admitted)
    for value in range(1, 6):
        assert isinstance(await store.admit_or_enqueue(f"{value:032x}", request("a")), Denied)
    with pytest.raises(ControlStoreError) as caught:
        await store.admit_or_enqueue("6" * 32, request("a"))
    assert caught.value.code == "store_capacity"
    await store.finalize(
        "7" * 32,
        admitted.lease.lease_id,
        admitted.lease.fencing_token,
        BudgetReconciliation(Decimal(0), "USD", False),
    )


@pytest.mark.asyncio
async def test_replay_uses_captured_method_and_fresh_request_copy() -> None:
    class Store:
        def __init__(self) -> None:
            self.calls = 0
            self.poison_calls = 0
            self.ids: list[str] = []

        async def admit_or_enqueue(self, operation_id: str, admission: AdmissionRequest) -> Denied:
            self.calls += 1
            self.ids.append(operation_id)
            if self.calls == 1:
                object.__setattr__(admission, "ip_digest", "PRIVATE-CANARY")
                self.admit_or_enqueue = self.poison  # type: ignore[assignment]
                raise ControlStoreError("store_timeout")
            assert admission.ip_digest != "PRIVATE-CANARY"
            return Denied("budget_exhausted")

        async def poison(self, *args: object) -> Denied:
            del args
            self.poison_calls += 1
            return Denied("budget_exhausted")

        async def wait_for_admission(self, *args: object) -> Denied:
            del args
            return Denied("budget_exhausted")

        async def renew(self, *args: object) -> Renewed:
            del args
            return Renewed("2026-09-11T12:00:30.000000Z")

        async def finalize(self, *args: object) -> Finalized:
            del args
            return Finalized()

        async def cancel_ticket(self, *args: object) -> object:
            del args
            return object()

        async def check_budget_readiness(self) -> object:
            return object()

    store = Store()
    controller = EndpointController(cast(Any, store), config())
    assert await controller.admit("192.0.2.1", (), "s", 1) == Denied("budget_exhausted")
    assert store.calls == 2
    assert store.poison_calls == 0
    assert store.ids[0] == store.ids[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("timer_fails", [False, True])
async def test_early_or_failed_timer_is_invariant_without_replay(timer_fails: bool) -> None:
    calls = 0
    blocker = asyncio.Event()

    async def operation() -> object:
        nonlocal calls
        calls += 1
        await blocker.wait()
        return object()

    async def sleeper(delay: float) -> None:
        del delay
        if timer_fails:
            raise RuntimeError("PRIVATE-SLEEP")

    with pytest.raises(ControlStoreError) as caught:
        await call_mutation_with_one_replay(
            operation,
            monotonic=lambda: 0.0,
            sleeper=sleeper,
        )
    assert caught.value.code == "controller_invariant"
    assert calls <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("end", "accepted"),
    [(10.0, False), (19.999999, False), (20.0, True), (20.000001, True)],
)
async def test_controller_sleep_requires_the_exact_monotonic_deadline(
    end: float, accepted: bool
) -> None:
    readings = iter((10.0, end))
    observed: list[float] = []

    async def sleeper(delay: float) -> None:
        observed.append(delay)

    controller = EndpointController(
        InMemoryEndpointControlStore(config(), clock=Clock()),
        config(),
        monotonic=readings.__next__,
        sleeper=sleeper,
    )
    if accepted:
        await controller.sleep(10.0)
    else:
        with pytest.raises(EndpointControllerError):
            await controller.sleep(10.0)
    assert observed == [10.0]


def test_sync_store_method_returning_awaitable_is_rejected_before_invocation() -> None:
    calls = 0

    class Store:
        def admit_or_enqueue(self, *args: object) -> object:
            nonlocal calls
            calls += 1
            del args
            return object()

        async def wait_for_admission(self, *args: object) -> object:
            del args
            return object()

        async def renew(self, *args: object) -> object:
            del args
            return object()

        async def finalize(self, *args: object) -> object:
            del args
            return object()

        async def cancel_ticket(self, *args: object) -> object:
            del args
            return object()

        async def check_budget_readiness(self) -> object:
            return object()

    with pytest.raises(ControlConfigurationError):
        EndpointController(cast(Any, Store()), config())
    assert calls == 0


@pytest.mark.asyncio
async def test_second_token_failure_does_not_advance_store_identity_state() -> None:
    calls = 0

    def failing_tokens() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("PRIVATE-TOKEN")
        return "a" * 32

    cfg = config(ip_capacity=10, session_capacity=10)
    store = InMemoryEndpointControlStore(cfg, clock=Clock(), token_factory=failing_tokens)
    with pytest.raises(ControlStoreError):
        await store.admit_or_enqueue("a" * 32, request("a"))
    assert cast(Any, store)._token_sequence == 0
    assert store.snapshot()["counts"]["total"] == 0


@pytest.mark.asyncio
async def test_upper_base_instant_allows_bounded_december_expiries() -> None:
    clock = Clock()
    clock.now = datetime(9999, 11, 30, 23, 59, 59, 999999, tzinfo=UTC)
    cfg = config(
        ip_capacity=10,
        session_capacity=10,
        rate_state_ttl_seconds=31 * 24 * 3600,
    )
    store = InMemoryEndpointControlStore(cfg, clock=clock)
    outcome = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(outcome, Admitted)
    receipt = cast(Any, store)._receipts["a" * 32]
    assert receipt.expires_at.startswith("9999-12-31T23:59:59")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("daily", "advance_hour", "charge", "expected_state"),
    [
        ("20", False, "4", "ready"),
        ("20", False, "4.000001", "not_ready"),
        ("10", True, "4", "ready"),
        ("10", True, "4.000001", "not_ready"),
    ],
)
async def test_budget_readiness_requires_full_next_request_reservation(
    daily: str, advance_hour: bool, charge: str, expected_state: str
) -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(
        config(
            ip_capacity=10,
            session_capacity=10,
            budget_hourly=Decimal("10"),
            budget_daily=Decimal(daily),
            budget_reserve_per_attempt=Decimal("6"),
        ),
        clock=clock,
    )
    admitted = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(admitted, Admitted)
    await store.finalize(
        "b" * 32,
        admitted.lease.lease_id,
        admitted.lease.fencing_token,
        BudgetReconciliation(Decimal(charge), "USD", False),
    )
    if advance_hour:
        clock.advance(3_600)
    readiness = cast(Any, await store.check_budget_readiness())
    assert readiness.state == expected_state
    assert readiness.reason == ("ready" if expected_state == "ready" else "budget_exhausted")


@pytest.mark.asyncio
async def test_malformed_store_outcome_is_released_before_fixed_controller_error() -> None:
    class Canary:
        pass

    class Store:
        def __init__(self, outcome: Denied) -> None:
            self.outcome = outcome

        async def admit_or_enqueue(self, *args: object) -> Denied:
            del args
            return self.outcome

        async def wait_for_admission(self, *args: object) -> Denied:
            del args
            return Denied("budget_exhausted")

        async def renew(self, *args: object) -> Lost:
            del args
            return Lost("lease_expired")

        async def finalize(self, *args: object) -> Finalized:
            del args
            return Finalized()

        async def cancel_ticket(self, *args: object) -> CancelledTicket:
            del args
            return CancelledTicket()

        async def check_budget_readiness(self) -> object:
            return object()

    async def capture() -> tuple[ControlStoreError, weakref.ReferenceType[Canary]]:
        canary = Canary()
        reference = weakref.ref(canary)
        outcome = Denied("budget_exhausted")
        object.__setattr__(outcome, "reason", canary)
        store = Store(outcome)
        controller = EndpointController(cast(Any, store), config())
        try:
            await controller.admit("192.0.2.1", (), "s", 1)
        except ControlStoreError as error:
            del controller, store, outcome, canary
            return error, reference
        raise AssertionError("malformed outcome accepted")

    error, reference = await capture()
    assert error.code == "malformed_store"
    assert reference() is None


def test_monotonic_failure_releases_controller_and_clock_canary_without_gc() -> None:
    class Canary:
        pass

    class BadClock:
        def __init__(self, canary: Canary) -> None:
            self.canary = canary

        def __call__(self) -> float:
            raise RuntimeError(self.canary)

    def capture() -> tuple[Exception, weakref.ReferenceType[Canary]]:
        canary = Canary()
        reference = weakref.ref(canary)
        clock = BadClock(canary)
        controller = EndpointController(
            InMemoryEndpointControlStore(config(), clock=Clock()),
            config(),
            monotonic=clock,
        )
        try:
            controller.monotonic()
        except Exception as error:
            del controller, clock, canary
            return error, reference
        raise AssertionError("bad monotonic clock accepted")

    error, reference = capture()
    assert str(error) == "EndpointControllerError()"
    assert reference() is None


@pytest.mark.asyncio
async def test_fractional_rate_refill_and_consume_ignore_ambient_decimal_context() -> None:
    clock = Clock()
    store = InMemoryEndpointControlStore(
        config(
            ip_capacity=1,
            session_capacity=1,
            ip_refill_per_minute=Decimal("0.6"),
            session_refill_per_minute=Decimal("0.6"),
        ),
        clock=clock,
    )
    first = await store.admit_or_enqueue("a" * 32, request("a"))
    assert isinstance(first, Admitted)
    await store.finalize(
        "b" * 32,
        first.lease.lease_id,
        first.lease.fencing_token,
        BudgetReconciliation(Decimal(0), "USD", False),
    )
    clock.advance(99.9999)
    with localcontext(Context(prec=1, rounding=ROUND_UP)):
        assert await store.admit_or_enqueue("c" * 32, request("a")) == Denied(
            "ip_rate_limited"
        )
    clock.advance(0.0001)
    with localcontext(Context(prec=1, rounding=ROUND_DOWN)):
        assert isinstance(await store.admit_or_enqueue("d" * 32, request("a")), Admitted)


def test_negative_zero_is_rejected_at_strict_decimal_boundaries() -> None:
    with pytest.raises(ControlConfigurationError):
        config(budget_reserve_per_attempt=Decimal("-0"))
    with pytest.raises(ControlStoreError):
        BudgetReconciliation(Decimal("-0"), "USD", False)


def test_receipt_json_rejects_iso_week_date_even_when_it_resolves_to_same_day() -> None:
    payload = json.loads(
        control_operation_receipt_to_json(_receipt("admit_or_enqueue", Admitted(_lease())))
    )
    payload["outcome"]["lease"]["attempt_date"] = "2026-W37-5"
    with pytest.raises(ControlStoreError):
        control_operation_receipt_from_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("receipt", "mutate"),
    [
        (
            _receipt("admit_or_enqueue", Admitted(_lease())),
            lambda payload: payload["outcome"]["lease"].__setitem__(
                "reservation_instant", "2026-09-11T12:00:01.000000Z"
            ),
        ),
        (
            _receipt(
                "admit_or_enqueue",
                Queued(ControlTicket("1" * 32, "2" * 32, "2026-09-11T12:00:30.000000Z")),
            ),
            lambda payload: payload["outcome"]["ticket"].__setitem__(
                "expires_at", "2026-09-11T12:00:00.000000Z"
            ),
        ),
        (
            _receipt("renew", Renewed("2026-09-11T12:00:30.000000Z")),
            lambda payload: payload["outcome"].__setitem__(
                "expires_at", "2026-09-11T12:00:00.000000Z"
            ),
        ),
    ],
)
def test_receipt_json_binds_nested_outcome_time_to_commit(
    receipt: ControlOperationReceipt, mutate: Any
) -> None:
    payload = json.loads(control_operation_receipt_to_json(receipt))
    mutate(payload)
    with pytest.raises(ControlStoreError):
        control_operation_receipt_from_json(json.dumps(payload))


@pytest.mark.asyncio
async def test_store_ingress_failure_releases_mutated_request_canary_without_gc() -> None:
    class Canary:
        pass

    async def capture() -> tuple[ControlStoreError, weakref.ReferenceType[Canary]]:
        canary = Canary()
        reference = weakref.ref(canary)
        admission = request("a")
        object.__setattr__(admission, "ip_digest", canary)
        store = InMemoryEndpointControlStore(config(), clock=Clock())
        try:
            await store.admit_or_enqueue("a" * 32, admission)
        except ControlStoreError as error:
            del store, admission, canary
            return error, reference
        raise AssertionError("mutated request accepted")

    error, reference = await capture()
    assert error.code == "malformed_store"
    assert reference() is None


@pytest.mark.asyncio
async def test_caller_cancellation_arriving_during_success_cleanup_is_preserved() -> None:
    readings = 0

    def monotonic() -> float:
        nonlocal readings
        readings += 1
        if readings == 2:
            current = asyncio.current_task()
            assert current is not None
            current.cancel("CALLER-CANCEL")
        return 0.0

    async def operation() -> str:
        return "SUCCESS"

    async def sleeper(delay: float) -> None:
        del delay
        await asyncio.Event().wait()

    with pytest.raises(asyncio.CancelledError) as caught:
        await call_mutation_with_one_replay(
            operation,
            monotonic=monotonic,
            sleeper=sleeper,
        )
    assert caught.value.args == ("CALLER-CANCEL",)
    current = asyncio.current_task()
    assert current is not None
    current.uncancel()


@pytest.mark.asyncio
async def test_second_clock_read_self_cancellation_is_controller_invariant() -> None:
    readings = 0

    def monotonic() -> float:
        nonlocal readings
        readings += 1
        if readings == 2:
            raise asyncio.CancelledError("CLOCK-SELF")
        return 0.0

    async def operation() -> str:
        return "SUCCESS"

    async def sleeper(delay: float) -> None:
        del delay
        await asyncio.Event().wait()

    with pytest.raises(ControlStoreError) as caught:
        await call_mutation_with_one_replay(
            operation,
            monotonic=monotonic,
            sleeper=sleeper,
        )
    assert caught.value.code == "controller_invariant"


@pytest.mark.asyncio
async def test_queued_ticket_ttl_ignores_ambient_decimal_context() -> None:
    store = InMemoryEndpointControlStore(
        config(
            ip_capacity=10,
            session_capacity=10,
            queue_wait_seconds=Decimal("1.234567"),
        ),
        clock=Clock(),
    )
    assert isinstance(await store.admit_or_enqueue("a" * 32, request("a")), Admitted)
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)):
        queued = await store.admit_or_enqueue("b" * 32, request("b"))
    assert isinstance(queued, Queued)
    assert queued.ticket.expires_at == "2026-09-11T12:00:11.234567Z"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["admit_or_enqueue", "wait_for_admission", "renew", "finalize", "cancel_ticket"],
)
async def test_every_mutation_replays_the_exact_committed_outcome_without_state_change(
    kind: str,
) -> None:
    store = InMemoryEndpointControlStore(
        config(ip_capacity=20, session_capacity=20),
        clock=Clock(),
    )
    operation_id = "f" * 32
    ticket: ControlTicket | None = None
    lease: ControlLease | None = None
    if kind in {"wait_for_admission", "cancel_ticket"}:
        assert isinstance(await store.admit_or_enqueue("a" * 32, request("active")), Admitted)
        queued = await store.admit_or_enqueue("b" * 32, request("queued"))
        assert isinstance(queued, Queued)
        ticket = queued.ticket
    elif kind in {"renew", "finalize"}:
        admitted = await store.admit_or_enqueue("a" * 32, request("active"))
        assert isinstance(admitted, Admitted)
        lease = admitted.lease

    async def call() -> object:
        if kind == "admit_or_enqueue":
            return await store.admit_or_enqueue(operation_id, request("target"))
        if kind == "wait_for_admission":
            assert ticket is not None
            return await store.wait_for_admission(
                operation_id, ticket.ticket_id, ticket.fencing_token
            )
        if kind == "cancel_ticket":
            assert ticket is not None
            return await store.cancel_ticket(
                operation_id, ticket.ticket_id, ticket.fencing_token
            )
        assert lease is not None
        if kind == "renew":
            return await store.renew(operation_id, lease.lease_id, lease.fencing_token)
        return await store.finalize(
                operation_id,
                lease.lease_id,
                lease.fencing_token,
                BudgetReconciliation(Decimal(0), "USD", False),
            )
    first = await call()
    committed = store.snapshot()
    second = await call()
    assert second == first
    assert second is not first
    assert store.snapshot() == committed


@pytest.mark.asyncio
async def test_store_enforces_exact_poll_and_renewal_logical_tick_caps() -> None:
    poll_store = InMemoryEndpointControlStore(
        config(ip_capacity=10, session_capacity=10, queue_wait_seconds=Decimal("2")),
        clock=Clock(),
    )
    assert isinstance(await poll_store.admit_or_enqueue("a" * 32, request("active")), Admitted)
    queued = await poll_store.admit_or_enqueue("b" * 32, request("queued"))
    assert isinstance(queued, Queued)
    for index in range(2):
        assert isinstance(
            await poll_store.wait_for_admission(
                f"{index + 2:032x}",
                queued.ticket.ticket_id,
                queued.ticket.fencing_token,
            ),
            Pending,
        )
    with pytest.raises(ControlStoreError) as poll_error:
        await poll_store.wait_for_admission(
            "4" * 32,
            queued.ticket.ticket_id,
            queued.ticket.fencing_token,
        )
    assert poll_error.value.code == "store_capacity"

    renew_store = InMemoryEndpointControlStore(
        config(ip_capacity=10, session_capacity=10, max_keys=500),
        clock=Clock(),
    )
    admitted = await renew_store.admit_or_enqueue("a" * 32, request("lease"))
    assert isinstance(admitted, Admitted)
    for index in range(CONTROL_MAX_RENEWAL_TICKS_PER_LEASE):
        assert isinstance(
            await renew_store.renew(
                f"{index + 1:032x}",
                admitted.lease.lease_id,
                admitted.lease.fencing_token,
            ),
            Renewed,
        )
    before = renew_store.snapshot()
    with pytest.raises(ControlStoreError) as renew_error:
        await renew_store.renew(
            "f" * 32,
            admitted.lease.lease_id,
            admitted.lease.fencing_token,
        )
    assert renew_error.value.code == "store_capacity"
    assert renew_store.snapshot() == before


@pytest.mark.asyncio
async def test_ticket_fifo_sequence_accepts_max_then_fails_atomically() -> None:
    store = InMemoryEndpointControlStore(
        config(ip_capacity=20, session_capacity=20),
        clock=Clock(),
    )
    active = await store.admit_or_enqueue("a" * 32, request("active"))
    assert isinstance(active, Admitted)
    store._sequence = CONTROL_MAX_INTEGER - 1

    queued = await store.admit_or_enqueue("b" * 32, request("queued-at-max"))
    assert isinstance(queued, Queued)
    assert store._sequence == CONTROL_MAX_INTEGER
    assert store._tickets[queued.ticket.ticket_id].sequence == CONTROL_MAX_INTEGER
    cancelled = await store.cancel_ticket(
        "c" * 32,
        queued.ticket.ticket_id,
        queued.ticket.fencing_token,
    )
    assert isinstance(cancelled, CancelledTicket)

    before = store.snapshot()
    token_sequence = store._token_sequence
    with pytest.raises(ControlStoreError) as caught:
        await store.admit_or_enqueue("d" * 32, request("queue-overflow"))
    assert caught.value.code == "store_capacity"
    assert store._sequence == CONTROL_MAX_INTEGER
    assert store._token_sequence == token_sequence
    assert store.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["admit_or_enqueue", "wait_for_admission", "renew", "finalize", "cancel_ticket"],
)
@pytest.mark.parametrize("retryable_code", ["store_timeout", "unknown_outcome"])
@pytest.mark.parametrize("commit_before_error", [False, True])
async def test_every_mutation_replays_one_unknown_with_same_id_and_fresh_input(
    kind: str, retryable_code: str, commit_before_error: bool
) -> None:
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:00:30.000000Z",
    )
    store = _MatrixStore(
        kind,
        ticket,
        errors=(retryable_code,),
        commit_before_error=commit_before_error,
    )

    async def sleeper(delay: float) -> None:
        del delay
        await asyncio.Event().wait()

    controller = EndpointController(
        cast(Any, store),
        config(),
        operation_id_factory=lambda: "a" * 32,
        monotonic=lambda: 0.0,
        sleeper=sleeper,
    )
    lease = _lease()
    if kind == "renew":
        _seed_matrix_lease(controller, lease)
    await _invoke_matrix_operation(controller, kind, ticket, lease)

    matching = [call for call in store.calls if call[0] == kind]
    assert len(matching) == 2
    assert matching[0][1] == matching[1][1]
    assert matching[0][2] == matching[1][2]
    if kind in {"admit_or_enqueue", "finalize"}:
        assert matching[0][2][-1] is not matching[1][2][-1]
    assert store.commits == int(commit_before_error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["admit_or_enqueue", "wait_for_admission", "renew", "finalize", "cancel_ticket"],
)
@pytest.mark.parametrize(
    "code",
    [
        "store_capacity",
        "malformed_store",
        "store_conflict",
        "store_unavailable",
        "fencing_lost",
        "lease_expired",
        "controller_invariant",
    ],
)
async def test_every_mutation_permanent_or_malformed_failure_never_replays(
    kind: str, code: str
) -> None:
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:00:30.000000Z",
    )
    store = _MatrixStore(kind, ticket, errors=(code,))
    controller = EndpointController(
        cast(Any, store),
        config(),
        operation_id_factory=lambda: "a" * 32,
        monotonic=lambda: 0.0,
    )
    lease = _lease()
    if kind == "renew":
        _seed_matrix_lease(controller, lease)
    with pytest.raises(ControlStoreError) as caught:
        await _invoke_matrix_operation(controller, kind, ticket, lease)
    assert caught.value.code == code
    assert len([call for call in store.calls if call[0] == kind]) == 1
    if kind == "renew":
        assert controller.authority_remaining(lease) == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "admit_or_enqueue",
        "wait_for_admission",
        "renew",
        "finalize",
        "cancel_ticket",
        "check_budget_readiness",
    ],
)
async def test_caller_cancellation_at_every_store_await_never_replays(kind: str) -> None:
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:00:30.000000Z",
    )
    store = _MatrixStore(kind, ticket, block=True)
    controller = EndpointController(
        cast(Any, store),
        config(),
        operation_id_factory=lambda: "a" * 32,
        monotonic=lambda: 0.0,
    )
    lease = _lease()
    if kind == "renew":
        _seed_matrix_lease(controller, lease)
    task = asyncio.create_task(_invoke_matrix_operation(controller, kind, ticket, lease))
    await store.started.wait()
    task.cancel("CALLER-CANCEL")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("CALLER-CANCEL",)
    await asyncio.sleep(0)
    assert len([call for call in store.calls if call[0] == kind]) == 1
    if kind == "renew":
        assert controller.authority_remaining(lease) == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remaining", "store_expiry_present", "expected_calls"),
    [(9.999999, True, 0), (10.0, True, 1), (30.0, False, 0)],
)
async def test_renew_hard_loss_always_revokes_local_authority(
    remaining: float, store_expiry_present: bool, expected_calls: int
) -> None:
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:00:30.000000Z",
    )
    store = _MatrixStore("renew", ticket)
    controller = EndpointController(
        cast(Any, store),
        config(),
        operation_id_factory=lambda: "a" * 32,
        monotonic=lambda: 10.0,
    )
    lease = _lease()
    key = (lease.lease_id, lease.fencing_token)
    cast(Any, controller)._lease_deadlines[key] = 10.0 + remaining
    if store_expiry_present:
        cast(Any, controller)._lease_store_expiries[key] = datetime(
            2026, 9, 11, 12, 0, 30, tzinfo=UTC
        )
    expected = Lost("fencing_lost" if expected_calls else "lease_expired")
    assert await controller.renew(lease) == expected
    assert len([call for call in store.calls if call[0] == "renew"]) == expected_calls
    assert controller.authority_remaining(lease) == 0.0
    assert key not in cast(Any, controller)._lease_deadlines
    assert key not in cast(Any, controller)._lease_store_expiries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_kind",
    [
        "admit_or_enqueue",
        "wait_for_admission",
        "renew",
        "finalize",
        "cancel_ticket",
        "check_budget_readiness",
    ],
)
@pytest.mark.parametrize(
    ("completion_time", "times_out"),
    [(4.999999, False), (5.0, True)],
)
async def test_store_call_deadline_is_exclusive_at_exact_five_seconds(
    operation_kind: str, completion_time: float, times_out: bool
) -> None:
    ticket = ControlTicket(
        "3" * 32,
        "4" * 32,
        "2026-09-11T12:00:30.000000Z",
    )

    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        async def admit_or_enqueue(self, operation_id: str, value: object) -> Denied:
            del value
            self.calls.append(("admit_or_enqueue", operation_id))
            return Denied("concurrency_limited")

        async def wait_for_admission(
            self, operation_id: str, ticket_id: str, fencing_token: str
        ) -> Pending:
            assert (ticket_id, fencing_token) == (ticket.ticket_id, ticket.fencing_token)
            self.calls.append(("wait_for_admission", operation_id))
            return Pending(ticket)

        async def renew(self, operation_id: str, *args: object) -> Lost:
            del args
            self.calls.append(("renew", operation_id))
            return Lost("fencing_lost")

        async def finalize(self, operation_id: str, *args: object) -> Finalized:
            del args
            self.calls.append(("finalize", operation_id))
            return Finalized()

        async def cancel_ticket(self, operation_id: str, *args: object) -> CancelledTicket:
            del args
            self.calls.append(("cancel_ticket", operation_id))
            return CancelledTicket()

        async def check_budget_readiness(self) -> object:
            self.calls.append(("check_budget_readiness", None))
            return object()

    leading_read = operation_kind in {"admit_or_enqueue", "wait_for_admission"}
    if times_out and operation_kind == "renew":
        values = [0.0, 0.0, 0.0, 5.0, 5.0, 5.0, 10.0]
    elif not times_out and operation_kind == "renew":
        values = [0.0, 0.0, 0.0, completion_time]
    elif times_out and operation_kind != "check_budget_readiness":
        values = ([0.0] if leading_read else []) + [0.0, 5.0, 5.0, 10.0]
    else:
        values = ([0.0] if leading_read else []) + [0.0, completion_time]
    readings = iter(values)

    async def sleeper(delay: float) -> None:
        del delay
        await asyncio.Event().wait()

    store = Store()
    controller = EndpointController(
        cast(Any, store),
        config(),
        operation_id_factory=lambda: "a" * 32,
        monotonic=readings.__next__,
        sleeper=sleeper,
    )
    lease = _lease()
    if operation_kind == "renew":
        key = (lease.lease_id, lease.fencing_token)
        cast(Any, controller)._lease_deadlines[key] = 100.0
        cast(Any, controller)._lease_store_expiries[key] = datetime(
            2026, 9, 11, 12, 0, 30, tzinfo=UTC
        )

    async def invoke() -> object:
        if operation_kind == "admit_or_enqueue":
            return await controller.admit("192.0.2.1", (), "session", 1)
        if operation_kind == "wait_for_admission":
            return await controller.poll(ticket)
        if operation_kind == "renew":
            return await controller.renew(lease)
        if operation_kind == "finalize":
            await controller.finalize_without_provider(lease)
            return None
        if operation_kind == "cancel_ticket":
            await controller.cancel(ticket)
            return None
        return await controller.check_readiness()

    if times_out:
        with pytest.raises(ControlStoreError) as caught:
            await invoke()
        assert caught.value.code == (
            "store_timeout"
            if operation_kind == "check_budget_readiness"
            else "unknown_outcome"
        )
    else:
        await invoke()
    expected_calls = 1 if not times_out or operation_kind == "check_budget_readiness" else 2
    matching = [call for call in store.calls if call[0] == operation_kind]
    assert len(matching) == expected_calls
    if expected_calls == 2:
        assert matching[0][1] == matching[1][1]
