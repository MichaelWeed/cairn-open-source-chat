"""Read-only selection of one exact retrieval route per operation."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from pydantic import BaseModel

from app.corpus_lifecycle import (
    AttestationTrustPolicy,
    CorpusLifecycleError,
    ResolvedActiveState,
    VerifiedLifecycleEvidence,
)
from app.ingest.candidate_persistence import (
    MAX_SIGNER_ID_CHARS,
    AttestationIdentity,
    AttestationVerifier,
    CandidatePersistenceError,
    VerifiedCandidateEvidence,
)
from app.ingest.planner import MAX_EMBEDDING_IDENTITY_CHARS
from app.retrieval_contracts import (
    MAX_SAFE_INTEGER,
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalAdapter,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalProbe,
    RetrievalScope,
)
from app.retrieval_firestore import FirestoreRetrievalAdapter

_MISSING = object()
_EVIDENCE_FIELDS = tuple(VerifiedLifecycleEvidence.model_fields)


def _content_free[Result](
    code: RetrievalErrorCode, operation: Callable[[], Result]
) -> Result:
    result: object = _MISSING
    try:
        result = operation()
    except Exception:
        pass
    if result is _MISSING:
        raise RetrievalError(code) from None
    return cast(Result, result)


def _reject_hidden_model_state(item: object) -> None:
    if isinstance(item, BaseModel):
        fields = type(item).model_fields
        if (
            set(object.__getattribute__(item, "__dict__")) != set(fields)
            or object.__getattribute__(item, "__pydantic_extra__")
        ):
            raise ValueError
        for name in fields:
            _reject_hidden_model_state(getattr(item, name))
    elif isinstance(item, (tuple, list)):
        for nested in item:
            _reject_hidden_model_state(nested)


def _copy_model[ModelT: BaseModel](value: object, model: type[ModelT]) -> ModelT:

    def copy() -> ModelT:
        if type(value) is not model:
            raise TypeError
        _reject_hidden_model_state(value)
        payload = {name: getattr(value, name) for name in model.model_fields}
        return model.model_validate(payload)

    return _content_free("invalid_request", copy)


def _copy_scope(value: object) -> RetrievalScope:
    if type(value) is LocalActiveScope:
        return _copy_model(value, LocalActiveScope)
    if type(value) is ExactCorpusReference:
        return _copy_model(value, ExactCorpusReference)
    raise RetrievalError("invalid_request") from None


def _printable(value: object, *, maximum: int) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError
    return value


def _dimensions(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2048:
        raise ValueError
    return value


def _adapter(value: object) -> RetrievalAdapter:
    if not isinstance(value, RetrievalAdapter):
        raise ValueError
    return value


@dataclass(frozen=True, slots=True)
class ExactRetrievalAdapterBinding:
    scope: ExactCorpusReference
    embedding_identity: str
    embedding_dimensions: int
    adapter: RetrievalAdapter

    def __post_init__(self) -> None:
        scope = _copy_model(self.scope, ExactCorpusReference)
        identity = _content_free(
            "invalid_request",
            lambda: _printable(
                self.embedding_identity,
                maximum=MAX_EMBEDDING_IDENTITY_CHARS,
            ),
        )
        dimensions = _content_free(
            "invalid_request", lambda: _dimensions(self.embedding_dimensions)
        )
        adapter = _content_free("invalid_request", lambda: _adapter(self.adapter))
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "embedding_identity", identity)
        object.__setattr__(self, "embedding_dimensions", dimensions)
        object.__setattr__(self, "adapter", adapter)


def _checked_firestore_binding(
    binding: object,
    *,
    requested_scope: ExactCorpusReference,
    embedding_identity: str | None = None,
    embedding_dimensions: int | None = None,
) -> ExactRetrievalAdapterBinding:
    def check() -> ExactRetrievalAdapterBinding:
        if type(binding) is not ExactRetrievalAdapterBinding:
            raise TypeError
        exact = binding
        if type(exact.adapter) is not FirestoreRetrievalAdapter:
            raise TypeError
        scope = _copy_model(exact.scope, ExactCorpusReference)
        expected_scope = _copy_model(requested_scope, ExactCorpusReference)
        descriptor_identity = _printable(
            exact.embedding_identity,
            maximum=MAX_EMBEDDING_IDENTITY_CHARS,
        )
        descriptor_dimensions = _dimensions(exact.embedding_dimensions)
        private_scope = _copy_model(
            object.__getattribute__(exact.adapter, "_scope"),
            ExactCorpusReference,
        )
        private_identity = _printable(
            object.__getattribute__(exact.adapter, "_embedding_identity"),
            maximum=MAX_EMBEDDING_IDENTITY_CHARS,
        )
        private_dimensions = _dimensions(
            object.__getattribute__(exact.adapter, "_embedding_dimensions")
        )
        if (
            scope != expected_scope
            or private_scope != expected_scope
            or private_scope != scope
            or private_identity != descriptor_identity
            or private_dimensions != descriptor_dimensions
            or (embedding_identity is not None and private_identity != embedding_identity)
            or (
                embedding_dimensions is not None
                and private_dimensions != embedding_dimensions
            )
        ):
            raise ValueError
        return ExactRetrievalAdapterBinding(
            scope=scope,
            embedding_identity=descriptor_identity,
            embedding_dimensions=descriptor_dimensions,
            adapter=exact.adapter,
        )

    return _content_free("malformed_result", check)


def binding_from_firestore_adapter(
    scope: ExactCorpusReference,
    adapter: RetrievalAdapter,
) -> ExactRetrievalAdapterBinding:
    """Build a public descriptor from only M6's three binding-authority fields."""

    def build() -> ExactRetrievalAdapterBinding:
        if type(adapter) is not FirestoreRetrievalAdapter:
            raise TypeError
        exact_scope = _copy_model(scope, ExactCorpusReference)
        identity = _printable(
            object.__getattribute__(adapter, "_embedding_identity"),
            maximum=MAX_EMBEDDING_IDENTITY_CHARS,
        )
        dimensions = _dimensions(object.__getattribute__(adapter, "_embedding_dimensions"))
        binding = ExactRetrievalAdapterBinding(
            scope=exact_scope,
            embedding_identity=identity,
            embedding_dimensions=dimensions,
            adapter=adapter,
        )
        return _checked_firestore_binding(binding, requested_scope=exact_scope)

    return _content_free("malformed_result", build)


@dataclass(frozen=True, slots=True)
class ResolvedRetrievalRoute:
    scope: RetrievalScope
    adapter: RetrievalAdapter
    exact_binding: ExactRetrievalAdapterBinding | None = None

    def __post_init__(self) -> None:
        scope = _copy_scope(self.scope)
        adapter = _content_free("invalid_request", lambda: _adapter(self.adapter))
        if type(scope) is LocalActiveScope:
            if self.exact_binding is not None:
                raise RetrievalError("invalid_request") from None
            binding = None
        else:
            if type(self.exact_binding) is not ExactRetrievalAdapterBinding:
                raise RetrievalError("invalid_request") from None
            if self.exact_binding.adapter is not adapter:
                raise RetrievalError("invalid_request") from None
            binding = _checked_firestore_binding(
                self.exact_binding,
                requested_scope=cast(ExactCorpusReference, scope),
            )
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "adapter", adapter)
        object.__setattr__(self, "exact_binding", binding)


def validate_route_authority(
    route: object,
    *,
    requested_scope: RetrievalScope | None = None,
) -> ResolvedRetrievalRoute:
    """Revalidate a returned route and, for exact routes, M6's private authority."""

    def validate() -> ResolvedRetrievalRoute:
        if type(route) is not ResolvedRetrievalRoute:
            raise TypeError
        exact = route
        validated = ResolvedRetrievalRoute(
            scope=exact.scope,
            adapter=exact.adapter,
            exact_binding=exact.exact_binding,
        )
        if requested_scope is not None:
            expected = _copy_scope(requested_scope)
            if validated.scope != expected:
                raise ValueError
        return validated

    return _content_free("malformed_result", validate)


class ExactRetrievalAdapterFactory(Protocol):
    def adapter_for(
        self,
        scope: ExactCorpusReference,
        *,
        embedding_identity: str,
        embedding_dimensions: int,
    ) -> ExactRetrievalAdapterBinding: ...


class ActiveStateResolver(Protocol):
    async def resolve_active_state(
        self,
        corpus_id: str,
        trust_policy: AttestationTrustPolicy,
    ) -> ResolvedActiveState: ...


@runtime_checkable
class RetrievalRouteResolver(Protocol):
    async def resolve_route(self) -> ResolvedRetrievalRoute: ...

    async def check_readiness(self) -> RetrievalProbe: ...


class StaticRetrievalRouteResolver:
    def __init__(self, route: ResolvedRetrievalRoute) -> None:
        if type(route) is not ResolvedRetrievalRoute:
            raise RetrievalError("invalid_request") from None
        self._route = validate_route_authority(route)

    async def resolve_route(self) -> ResolvedRetrievalRoute:
        return validate_route_authority(self._route)

    async def check_readiness(self) -> RetrievalProbe:
        route = await self.resolve_route()
        route = validate_route_authority(route, requested_scope=route.scope)
        failure_code: str | None = None
        raw: object = _MISSING
        try:
            raw = await route.adapter.check_readiness(route.scope)
        except asyncio.CancelledError:
            raise
        except RetrievalError as error:
            failure_code = error.code
        except Exception:
            failure_code = "malformed_result"
        if failure_code is not None:
            raise RetrievalError(cast(RetrievalErrorCode, failure_code)) from None
        if type(route.scope) is LocalActiveScope:
            def local_probe() -> RetrievalProbe:
                if type(raw) is not RetrievalProbe:
                    raise TypeError
                _reject_hidden_model_state(raw)
                if (
                    raw.scope != route.scope
                    or type(raw.reachable) is not bool
                    or type(raw.store_ready) is not bool
                    or type(raw.exact_version_ready) is not bool
                    or (not raw.reachable and raw.store_ready)
                ):
                    raise ValueError
                return RetrievalProbe(
                    scope=route.scope,
                    reachable=raw.reachable,
                    store_ready=raw.store_ready,
                    exact_version_ready=False,
                )

            return _content_free("malformed_result", local_probe)
        probe = _content_free("malformed_result", lambda: _copy_model(raw, RetrievalProbe))
        if probe.scope != route.scope:
            raise RetrievalError("malformed_result") from None
        return probe


class LifecycleRetrievalRouteResolver:
    def __init__(
        self,
        *,
        corpus_id: str,
        active_state_resolver: ActiveStateResolver,
        verify_attested_candidate: Callable[
            [ExactCorpusReference, AttestationIdentity, AttestationVerifier],
            Awaitable[VerifiedCandidateEvidence],
        ],
        trust_policy_supplier: Callable[[], AttestationTrustPolicy],
        expected_embedding_identity: str,
        expected_embedding_dimensions: int,
        adapter_factory: ExactRetrievalAdapterFactory,
    ) -> None:
        def validate() -> tuple[str, str, int]:
            if type(corpus_id) is not str:
                raise TypeError
            ExactCorpusReference(corpus_id=corpus_id, corpus_version="v1")
            if not callable(verify_attested_candidate) or not callable(trust_policy_supplier):
                raise TypeError
            if not callable(getattr(active_state_resolver, "resolve_active_state", None)):
                raise TypeError
            if not callable(getattr(adapter_factory, "adapter_for", None)):
                raise TypeError
            identity = _printable(
                expected_embedding_identity,
                maximum=MAX_EMBEDDING_IDENTITY_CHARS,
            )
            return corpus_id, identity, _dimensions(expected_embedding_dimensions)

        validated_id, validated_identity, validated_dimensions = _content_free(
            "invalid_request", validate
        )
        self._corpus_id = validated_id
        self._active_state_resolver = active_state_resolver
        self._verify_attested_candidate = verify_attested_candidate
        self._trust_policy_supplier = trust_policy_supplier
        self._expected_embedding_identity = validated_identity
        self._expected_embedding_dimensions = validated_dimensions
        self._adapter_factory = adapter_factory
        self._refresh_lock = asyncio.Lock()
        self._last_good_fingerprint: tuple[object, ...] | None = None
        self._last_policy_pair: tuple[str, int] | None = None

    def _policy_fingerprint(
        self,
        policy: AttestationTrustPolicy,
        state: ResolvedActiveState,
    ) -> tuple[tuple[object, ...], tuple[str, int]]:
        def capture() -> tuple[tuple[object, ...], tuple[str, int]]:
            version = _printable(policy.policy_version, maximum=MAX_SIGNER_ID_CHARS)
            generation = policy.policy_generation
            if type(generation) is not int or not 0 <= generation <= MAX_SAFE_INTEGER:
                raise ValueError
            evidence = state.evidence
            fingerprint = (
                state.target.kind,
                state.target.corpus_id,
                state.target.corpus_version,
                state.pointer_revision,
                evidence.attestation_payload_sha256,
                evidence.signature_algorithm_id,
                evidence.signing_key_id,
                version,
                generation,
            )
            return fingerprint, (version, generation)

        return _content_free("store_unavailable", capture)

    def _validate_policy_rotation(self, pair: tuple[str, int]) -> None:
        previous = self._last_policy_pair
        if previous is None:
            return
        previous_version, previous_generation = previous
        version, generation = pair
        if (
            generation < previous_generation
            or (generation == previous_generation and version != previous_version)
            or (generation > previous_generation and version == previous_version)
        ):
            raise RetrievalError("store_unavailable") from None

    async def _active_state(
        self, policy: AttestationTrustPolicy
    ) -> ResolvedActiveState:
        raw: object = _MISSING
        failed = False
        try:
            raw = await self._active_state_resolver.resolve_active_state(
                self._corpus_id,
                policy,
            )
        except asyncio.CancelledError:
            raise
        except CorpusLifecycleError:
            failed = True
        except Exception:
            failed = True
        if failed:
            raise RetrievalError("store_unavailable") from None
        return _content_free(
            "store_unavailable",
            lambda: _copy_model(raw, ResolvedActiveState),
        )

    def _identity_and_verifier(
        self,
        policy: AttestationTrustPolicy,
        state: ResolvedActiveState,
    ) -> tuple[AttestationIdentity, AttestationVerifier]:
        def select() -> tuple[AttestationIdentity, AttestationVerifier]:
            identity = AttestationIdentity(
                algorithm_id=state.evidence.signature_algorithm_id,
                key_id=state.evidence.signing_key_id,
            )
            verifier = policy.verifier_for(identity)
            if verifier is None:
                raise ValueError
            algorithm_id = _printable(
                verifier.algorithm_id,
                maximum=MAX_SIGNER_ID_CHARS,
            )
            key_id = _printable(
                verifier.key_id,
                maximum=MAX_SIGNER_ID_CHARS,
            )
            if (
                algorithm_id != identity.algorithm_id
                or key_id != identity.key_id
                or not callable(getattr(verifier, "verify", None))
            ):
                raise ValueError
            return identity, verifier

        return _content_free("store_unavailable", select)

    async def _verify_refresh(
        self,
        policy: AttestationTrustPolicy,
        state: ResolvedActiveState,
    ) -> None:
        identity, verifier = self._identity_and_verifier(policy, state)
        raw: object = _MISSING
        failed = False
        try:
            raw = await self._verify_attested_candidate(state.target, identity, verifier)
        except asyncio.CancelledError:
            raise
        except CandidatePersistenceError:
            failed = True
        except Exception:
            failed = True
        if failed:
            raise RetrievalError("store_unavailable") from None

        def verify() -> None:
            evidence = _copy_model(raw, VerifiedCandidateEvidence)
            payload = {field: getattr(evidence, field) for field in _EVIDENCE_FIELDS}
            projected = VerifiedLifecycleEvidence.model_validate(payload)
            if projected != state.evidence:
                raise ValueError

        _content_free("store_unavailable", verify)

    def _factory_binding(self, state: ResolvedActiveState) -> ExactRetrievalAdapterBinding:
        raw: object = _MISSING
        failed = False
        try:
            raw = self._adapter_factory.adapter_for(
                state.target,
                embedding_identity=self._expected_embedding_identity,
                embedding_dimensions=self._expected_embedding_dimensions,
            )
        except Exception:
            failed = True
        if failed:
            raise RetrievalError("malformed_result") from None
        return _checked_firestore_binding(
            raw,
            requested_scope=state.target,
            embedding_identity=self._expected_embedding_identity,
            embedding_dimensions=self._expected_embedding_dimensions,
        )

    async def resolve_route(self) -> ResolvedRetrievalRoute:
        policy: object = _MISSING
        supplier_failed = False
        try:
            policy = self._trust_policy_supplier()
        except Exception:
            supplier_failed = True
        if supplier_failed:
            raise RetrievalError("store_unavailable") from None
        selected_policy = cast(AttestationTrustPolicy, policy)
        state = await self._active_state(selected_policy)
        if (
            state.evidence.embedding_identity != self._expected_embedding_identity
            or state.evidence.embedding_dimensions != self._expected_embedding_dimensions
        ):
            raise RetrievalError("store_unavailable") from None
        fingerprint, pair = self._policy_fingerprint(selected_policy, state)
        self._validate_policy_rotation(pair)
        if self._last_good_fingerprint != fingerprint:
            async with self._refresh_lock:
                self._validate_policy_rotation(pair)
                if self._last_good_fingerprint != fingerprint:
                    await self._verify_refresh(selected_policy, state)
                    self._last_good_fingerprint = fingerprint
                    self._last_policy_pair = pair
        binding = self._factory_binding(state)
        return validate_route_authority(
            ResolvedRetrievalRoute(
                scope=state.target,
                adapter=binding.adapter,
                exact_binding=binding,
            ),
            requested_scope=state.target,
        )

    async def check_readiness(self) -> RetrievalProbe:
        route = await self.resolve_route()
        route = validate_route_authority(route, requested_scope=route.scope)
        failure_code: str | None = None
        raw: object = _MISSING
        try:
            raw = await route.adapter.check_readiness(route.scope)
        except asyncio.CancelledError:
            raise
        except RetrievalError as error:
            failure_code = error.code
        except Exception:
            failure_code = "malformed_result"
        if failure_code is not None:
            raise RetrievalError(cast(RetrievalErrorCode, failure_code)) from None
        probe = _content_free("malformed_result", lambda: _copy_model(raw, RetrievalProbe))
        if probe.scope != route.scope:
            raise RetrievalError("malformed_result") from None
        return RetrievalProbe(
            scope=route.scope,
            reachable=probe.reachable,
            store_ready=probe.store_ready,
            exact_version_ready=probe.reachable and probe.store_ready,
        )
