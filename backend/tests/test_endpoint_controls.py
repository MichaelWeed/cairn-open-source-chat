from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.endpoint_controls import (
    CONTROL_MAX_RENEWAL_TICKS_PER_LEASE,
    CONTROL_QUEUE_POLL_INTERVAL_SECONDS,
    AdmissionRequest,
    Admitted,
    BudgetReconciliation,
    ClientIdentityError,
    ControlConfigurationError,
    ControlStoreError,
    Denied,
    EndpointControlConfig,
    InMemoryEndpointControlStore,
    Pending,
    Queued,
    Renewed,
    canonical_utc_instant,
    decode_digest_key,
    endpoint_control_config_from_settings,
    opaque_identity_digest,
    resolve_client_identity,
)
from app.main import create_app


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
