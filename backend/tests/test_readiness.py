import asyncio
import hashlib
import json
import logging
import os
import socket
import weakref
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal, cast

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from app.config import Settings
from app.ingest.startup import CorpusStartupError
from app.main import create_app
from app.providers.echo import EchoProvider
from app.providers.gemini import GeminiReadiness
from app.readiness import (
    OLLAMA_CATALOG_MAX_MODELS,
    OLLAMA_CATALOG_MAX_RESPONSE_BYTES,
    OLLAMA_MODEL_NAME_MAX_CHARS,
    BudgetReadiness,
    GeminiReadinessProbe,
    OllamaCatalogProbe,
    OllamaCatalogReadiness,
    OllamaCatalogReadinessProbe,
    ReadinessCheck,
    ReadinessDimension,
    ReadinessError,
    ReadinessEvaluator,
    ReadinessReport,
    RetrievalReadinessProfile,
    public_readiness,
)
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalProbe,
)

DIMENSIONS = (
    "database",
    "vector_store",
    "corpus",
    "provider",
    "model",
    "embedding",
    "exact_corpus",
    "budget",
)


def test_accepted_m10_and_widget_frozen_surfaces_are_byte_identical() -> None:
    root = Path(__file__).resolve().parents[2]
    accepted = {
        "backend/app/api/chat.py": (
            "11c673ac05e38056b179b7502896e39cde08ae9d279e2e442fdbe97923d6d36a"
        ),
        "backend/app/retrieval.py": (
            "195aa0b08148793394e66c09df68783359ccf39c80a6748138f989225bd86914"
        ),
        "backend/app/retrieval_integrity.py": (
            "f1e1c880848749fd4e0c9147f3787a1563048a0f92b194d10cbbd851eec90333"
        ),
        "backend/app/api/contracts.py": (
            "1895eaf57db82f12eac3855de603e3e686c1e75c010430addca537ed260a2cb1"
        ),
        "backend/app/retrieval_contracts.py": (
            "fae47b51302018c364a5423b800f754c39f7beba138505399e49b728ca9831af"
        ),
        "widget/src/index.ts": (
            "fe5727fba0ee4a410c2b30f62cb728e9eee67c68ac040115fe922b7492309ee7"
        ),
        "widget/src/protocol.ts": (
            "cd4c993803e0ac05e4dd8b7f19b138d0eb814907f4e6200a51f17ee37506ec58"
        ),
        "backend/app/static/widget/widget.js": (
            "bb170dfe776e16a07c9ee51cd8ff4958e58e9caf84d10c0b1475b31e299bc9a2"
        ),
    }
    for relative, expected in accepted.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected


def test_chat_byte_sentinel_rejects_one_byte_mutation() -> None:
    chat_bytes = (Path(__file__).resolve().parents[2] / "backend/app/api/chat.py").read_bytes()
    accepted = "11c673ac05e38056b179b7502896e39cde08ae9d279e2e442fdbe97923d6d36a"
    assert hashlib.sha256(chat_bytes + b"\x00").hexdigest() != accepted


def _ready_check(dimension: str) -> ReadinessCheck:
    return ReadinessCheck(
        dimension=cast(ReadinessDimension, dimension),
        state="ready",
        required=True,
        reason="ready",
    )


def test_report_derives_ready_and_requires_exact_order() -> None:
    checks = tuple(_ready_check(dimension) for dimension in DIMENSIONS)
    report = ReadinessReport(checks=checks)
    assert report.ready is True
    assert tuple(check.dimension for check in report.checks) == DIMENSIONS
    with pytest.raises(ReadinessError):
        ReadinessReport(checks=tuple(reversed(checks)))
    with pytest.raises(ReadinessError):
        ReadinessReport(checks=checks, ready=False)


@pytest.mark.parametrize(
    ("values", "valid"),
    [
        ({"dimension": "budget", "state": "not_required", "required": False,
          "reason": "not_required"}, True),
        ({"dimension": "budget", "state": "not_required", "required": True,
          "reason": "not_required"}, False),
        ({"dimension": "model", "state": "not_ready", "required": True,
          "reason": "model_missing"}, True),
        ({"dimension": "model", "state": "not_ready", "required": True,
          "reason": "store_unready"}, False),
        ({"dimension": "provider", "state": "unknown", "required": True,
          "reason": "unavailable"}, True),
    ],
)
def test_check_truth_table_is_closed(values: dict[str, object], valid: bool) -> None:
    if valid:
        assert ReadinessCheck.model_validate(values).model_dump() == {
            "contract_version": "1.0",
            **values,
        }
    else:
        with pytest.raises(ReadinessError):
            ReadinessCheck.model_validate(values)


def test_structural_bypasses_fail_content_free() -> None:
    canary = "private-readiness-canary"
    with pytest.raises(ReadinessError) as caught:
        ReadinessCheck.model_construct(
            dimension="provider",
            state="ready",
            required=True,
            reason=canary,
        )
    rendered = (
        str(caught.value)
        + repr(caught.value)
        + repr(caught.value.args)
        + repr(vars(caught.value))
        + repr(caught.value.errors())
        + caught.value.json()
        + repr(caught.value.__cause__)
        + repr(caught.value.__context__)
    )
    assert canary not in rendered


def _assert_content_free(error: BaseException, *canaries: str) -> None:
    pending: list[object] = [error]
    seen: set[int] = set()
    rendered: list[str] = []
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        rendered.extend((str(value), repr(value)))
        if isinstance(value, BaseException):
            if isinstance(value, ReadinessError):
                assert value.__traceback__ is None
            pending.extend((value.args, value.__cause__, value.__context__, vars(value)))
            if hasattr(value, "errors"):
                pending.append(cast(Any, value).errors(include_input=True))
            if hasattr(value, "json"):
                pending.append(cast(Any, value).json(include_input=True))
        elif type(value) is dict:
            pending.extend(dict.keys(value))
            pending.extend(dict.values(value))
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(value)
    combined = "\n".join(rendered)
    for canary in canaries:
        assert canary not in combined


@pytest.mark.parametrize(
    "model",
    [ReadinessCheck, ReadinessReport, OllamaCatalogReadiness, BudgetReadiness],
)
def test_readiness_models_seal_validation_copy_construct_and_assignment(
    model: type[Any],
) -> None:
    check = _ready_check("database")
    instance: Any
    if model is ReadinessCheck:
        instance = check
    elif model is ReadinessReport:
        instance = ReadinessReport(
            checks=tuple(_ready_check(dimension) for dimension in DIMENSIONS)
        )
    elif model is OllamaCatalogReadiness:
        instance = OllamaCatalogReadiness(reachable=True, model_names=("model-a",))
    else:
        instance = BudgetReadiness(state="ready", reason="ready")
    values = instance.model_dump(mode="python", round_trip=True)
    json_values = instance.model_dump(mode="json", round_trip=True)
    canary = f"PRIVATE-{model.__name__}-CANARY"
    malformed = "{" + canary
    structural_json = json.dumps({**json_values, canary: canary})
    adapter = TypeAdapter(model)
    routes: tuple[Callable[[], object], ...] = (
        lambda: model(**{**values, canary: canary}),
        lambda: model.model_validate({**values, canary: canary}, extra="allow"),
        lambda: model.model_validate_json(malformed),
        lambda: model.model_validate_json(structural_json),
        lambda: model.model_validate_strings({**json_values, canary: canary}),
        lambda: adapter.validate_python({**values, canary: canary}),
        lambda: adapter.validate_json(malformed),
        lambda: adapter.validate_json(structural_json),
        lambda: adapter.validate_strings({**json_values, canary: canary}),
        lambda: model.model_construct(**{**values, canary: canary}),
        lambda: model.construct(**{**values, canary: canary}),
        lambda: instance.model_copy(update={canary: canary}),
        lambda: instance.copy(update={canary: canary}),
        lambda: instance.__replace__(**{canary: canary}),
    )
    for route in routes:
        with pytest.raises(ReadinessError) as caught:
            route()
        _assert_content_free(caught.value, canary, malformed)
    with pytest.raises(ReadinessError) as caught:
        setattr(instance, next(iter(values)), canary)
    _assert_content_free(caught.value, canary)
    assert model.model_validate(values) == instance
    assert model.model_validate_json(instance.model_dump_json()) == instance
    assert model.model_construct(**values) == instance
    assert instance.model_copy() == instance
    assert instance.__replace__() == instance


def test_report_revalidates_nested_instances_and_public_projection() -> None:
    checks = tuple(_ready_check(dimension) for dimension in DIMENSIONS)
    report = ReadinessReport(checks=checks)
    assert public_readiness(report) == (
        True,
        {"database": True, "vector_store": True, "corpus": True},
    )
    forged = checks[2].model_copy()
    object.__setattr__(forged, "reason", "PRIVATE-NESTED-CANARY")
    with pytest.raises(ReadinessError) as caught:
        ReadinessReport(checks=(*checks[:2], forged, *checks[3:]))
    _assert_content_free(caught.value, "PRIVATE-NESTED-CANARY")
    object.__setattr__(report, "ready", False)
    with pytest.raises(ReadinessError):
        public_readiness(report)


def test_model_copy_rejects_hidden_source_state() -> None:
    canary = "PRIVATE-HIDDEN-COPY-CANARY"
    check = _ready_check("provider")
    object.__setattr__(check, canary, canary)
    for operation in (
        check.model_copy,
        check.copy,
        check.__replace__,
    ):
        with pytest.raises(ReadinessError) as caught:
            operation()
        _assert_content_free(caught.value, canary)

    class _HostileUpdate(dict[str, object]):
        def keys(self) -> Any:
            raise AssertionError(canary)

    with pytest.raises(ReadinessError) as caught:
        check.copy(update=_HostileUpdate(state="not_ready"))
    _assert_content_free(caught.value, canary)


def test_model_copy_rejects_oversized_and_hostile_exact_dict_updates_first() -> None:
    calls: list[str] = []

    class _HostileKey(str):
        def __hash__(self) -> int:
            calls.append("hash")
            return super().__hash__()

        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            return super().__eq__(other)

    check = _ready_check("provider")
    key = _HostileKey("state")
    hostile_update = {key: "not_ready"}
    calls.clear()
    with pytest.raises(ReadinessError):
        check.model_copy(update=cast(dict[str, Any], hostile_update))
    assert calls == []

    oversized = {f"extra-{index}": index for index in range(100_000)}
    with pytest.raises(ReadinessError):
        check.model_copy(update=oversized)
    with pytest.raises(ReadinessError):
        ReadinessCheck.model_validate(oversized)


def test_oversized_nested_collections_reject_before_element_hooks() -> None:
    calls: list[str] = []

    class _HostileName(str):
        def __hash__(self) -> int:
            calls.append("hash")
            raise AssertionError("oversized model name was inspected")

        def __eq__(self, other: object) -> bool:
            del other
            calls.append("eq")
            raise AssertionError("oversized model name was inspected")

    name = _HostileName("PRIVATE-OVERSIZED-NAME")
    forged = OllamaCatalogReadiness(reachable=True, model_names=())
    object.__setattr__(
        forged,
        "model_names",
        (name,) + ("safe",) * OLLAMA_CATALOG_MAX_MODELS,
    )
    operations: tuple[Callable[[], object], ...] = (
        forged.model_copy,
        lambda: OllamaCatalogReadiness.model_validate(forged),
        lambda: TypeAdapter(OllamaCatalogReadiness).validate_python(forged),
    )
    for operation in operations:
        with pytest.raises(ReadinessError):
            operation()
    assert calls == []

    with pytest.raises(ReadinessError):
        ReadinessReport.model_validate(
            {"checks": [object()] * (len(DIMENSIONS) + 1)}
        )


def test_private_model_state_is_rejected_directly_nested_and_on_copy() -> None:
    canary = "PRIVATE-PYDANTIC-PRIVATE-CANARY"
    check = _ready_check("database")
    object.__setattr__(check, "__pydantic_private__", {"x": canary})
    operations = (
        lambda: ReadinessCheck.model_validate(check),
        lambda: TypeAdapter(ReadinessCheck).validate_python(check),
        check.model_copy,
        lambda: ReadinessReport(
            checks=(
                check,
                *tuple(_ready_check(dimension) for dimension in DIMENSIONS[1:]),
            )
        ),
    )
    for operation in operations:
        with pytest.raises(ReadinessError) as caught:
            operation()
        _assert_content_free(caught.value, canary)


def test_forged_fields_set_is_rejected_directly_and_when_nested() -> None:
    canary = "PRIVATE-FIELDS-SET-CANARY"
    check = _ready_check("database")
    object.__setattr__(check, "__pydantic_fields_set__", {canary})
    for operation in (
        lambda: ReadinessCheck.model_validate(check),
        lambda: TypeAdapter(ReadinessCheck).validate_python(check),
        check.model_copy,
        lambda: ReadinessReport(
            checks=(
                check,
                *tuple(_ready_check(dimension) for dimension in DIMENSIONS[1:]),
            )
        ),
    ):
        with pytest.raises(ReadinessError) as caught:
            operation()
        _assert_content_free(caught.value, canary)

    report = ReadinessReport(
        checks=tuple(_ready_check(dimension) for dimension in DIMENSIONS)
    )
    object.__setattr__(report, "__pydantic_fields_set__", {canary})
    with pytest.raises(ReadinessError) as caught:
        public_readiness(report)
    _assert_content_free(caught.value, canary)

    oversized = _ready_check("database")
    object.__setattr__(
        oversized,
        "__pydantic_fields_set__",
        {f"field-{index}" for index in range(100_000)},
    )
    with pytest.raises(ReadinessError):
        oversized.model_copy()


async def test_fields_set_str_subclass_never_invokes_hooks_across_boundaries() -> None:
    calls: list[str] = []

    class _HostileField(str):
        def __hash__(self) -> int:
            calls.append("hash")
            return super().__hash__()

        def __eq__(self, other: object) -> bool:
            del other
            calls.append("eq")
            raise AssertionError("fields-set equality hook invoked")

    def hostile_fields(name: str) -> set[str]:
        fields = cast(set[str], {_HostileField(name)})
        calls.clear()
        return fields

    check = _ready_check("database")
    object.__setattr__(check, "__pydantic_fields_set__", hostile_fields("dimension"))
    for operation in (
        lambda: ReadinessCheck.model_validate(check),
        check.model_copy,
        lambda: ReadinessReport(
            checks=(
                check,
                *tuple(_ready_check(dimension) for dimension in DIMENSIONS[1:]),
            )
        ),
    ):
        with pytest.raises(ReadinessError):
            operation()
        assert calls == []

    catalog = OllamaCatalogReadiness(reachable=True, model_names=("model-a",))
    object.__setattr__(catalog, "__pydantic_fields_set__", hostile_fields("reachable"))
    evaluator, _ = _evaluator(
        provider="ollama", provider_model="model-a", catalog_probe=_CatalogProbe(catalog)
    )
    assert (await evaluator.evaluate()).checks[3].reason == "unavailable"
    assert calls == []

    scope = LocalActiveScope()
    object.__setattr__(scope, "__pydantic_fields_set__", hostile_fields("contract_version"))
    with pytest.raises(ReadinessError):
        _evaluator(expected_scope=scope)
    assert calls == []

    probe = RetrievalProbe(
        scope=LocalActiveScope(),
        reachable=True,
        store_ready=True,
        exact_version_ready=False,
    )
    object.__setattr__(probe, "__pydantic_fields_set__", hostile_fields("reachable"))
    route_evaluator, _ = _evaluator(route_result=probe)
    route_report = await route_evaluator.evaluate()
    assert route_report.checks[1].reason == "misconfigured"
    assert calls == []

    report = ReadinessReport(
        checks=tuple(_ready_check(dimension) for dimension in DIMENSIONS)
    )
    object.__setattr__(report, "__pydantic_fields_set__", hostile_fields("checks"))
    with pytest.raises(ReadinessError):
        public_readiness(report)
    assert calls == []


def test_model_construct_rejects_stateful_fields_set_without_consuming_it() -> None:
    canary = "PRIVATE-STATEFUL-FIELDS-CANARY"

    class _StatefulFields:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self) -> object:
            self.iterations += 1
            return iter(("dimension",) if self.iterations == 1 else (canary,))

    fields = _StatefulFields()
    with pytest.raises(ReadinessError) as caught:
        ReadinessCheck.model_construct(
            _fields_set=cast(Any, fields),
            dimension="database",
            state="ready",
            required=True,
            reason="ready",
        )
    _assert_content_free(caught.value, canary)
    assert fields.iterations == 0


class _StaticRouteProbe:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def check_readiness(self) -> object:
        self.calls += 1
        return self.result


class _GeminiProbe:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def check_readiness(self) -> object:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _CatalogProbe:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def check_readiness(self) -> object:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        raise AssertionError("borrowed catalog probe must not close")


def _evaluator(
    *,
    profile: RetrievalReadinessProfile = "local_static",
    provider: Literal["echo", "ollama", "gemini"] = "echo",
    provider_model: str = "",
    embedding: Literal["fake", "ollama"] = "fake",
    embedding_model: str = "",
    route_result: object | None = None,
    gemini_probe: object | None = None,
    catalog_probe: object | None = None,
    vector_failure: bool = False,
    composition_valid: bool = True,
    expected_scope: object = None,
    vector_calls: list[bool] | None = None,
    budget_probe: object | None = None,
) -> tuple[ReadinessEvaluator, _StaticRouteProbe]:
    scope = (
        ExactCorpusReference(corpus_id="public-docs", corpus_version="v1")
        if profile != "local_static"
        else LocalActiveScope()
    )
    route = _StaticRouteProbe(
        route_result
        if route_result is not None
        else RetrievalProbe(
            scope=scope,
            reachable=True,
            store_ready=True,
            exact_version_ready=profile == "lifecycle_exact",
        )
    )

    def vector_probe() -> None:
        if vector_calls is not None:
            vector_calls.append(True)
        if vector_failure:
            raise RuntimeError("private-vector-canary")

    evaluator = ReadinessEvaluator(
        database_probe=lambda: True,
        corpus_probe=lambda: True,
        local_vector_probe=vector_probe,
        retrieval_route_resolver=route,
        retrieval_profile=profile,
        expected_retrieval_scope=(
            scope if expected_scope is None else cast(Any, expected_scope)
        ),
        provider_name=provider,
        provider_model=provider_model,
        embedding_name=embedding,
        embedding_model=embedding_model,
        gemini_probe=cast(GeminiReadinessProbe | None, gemini_probe),
        ollama_catalog_probe=cast(OllamaCatalogReadinessProbe | None, catalog_probe),
        budget_probe=cast(Any, budget_probe),
        retrieval_composition_valid=composition_valid,
    )
    return evaluator, route


async def test_echo_fake_local_profile_is_ready_without_external_probe() -> None:
    evaluator, route = _evaluator()
    report = await evaluator.evaluate()
    assert [(check.dimension, check.state, check.required) for check in report.checks] == [
        ("database", "ready", True),
        ("vector_store", "ready", True),
        ("corpus", "ready", True),
        ("provider", "ready", True),
        ("model", "not_required", False),
        ("embedding", "ready", True),
        ("exact_corpus", "not_required", False),
        ("budget", "not_required", False),
    ]
    assert report.ready is True
    assert route.calls == 1


def test_evaluator_composition_authority_is_immutable() -> None:
    evaluator, _ = _evaluator()
    for name, value in (
        ("_provider_name", "ollama"),
        ("_retrieval_profile", "lifecycle_exact"),
        ("_retrieval_composition_valid", False),
    ):
        with pytest.raises(ReadinessError):
            setattr(evaluator, name, value)
        with pytest.raises(ReadinessError):
            delattr(evaluator, name)


async def test_gemini_false_is_authoritative_and_outer_failure_is_unknown() -> None:
    for result, expected in (
        (GeminiReadiness(reachable=False, model_ready=False), "not_ready"),
        (RuntimeError("private-gemini-canary"), "unknown"),
    ):
        probe = _GeminiProbe(result)
        evaluator, _ = _evaluator(
            provider="gemini",
            provider_model="gemini-3.8-flash",
            gemini_probe=probe,
        )
        report = await evaluator.evaluate()
        assert report.checks[3].state == expected
        assert probe.calls == 1


@pytest.mark.parametrize("kind", ["absent", "non_async", "wrong_signature"])
async def test_route_probe_interface_mismatch_is_misconfigured(kind: str) -> None:
    class _NonAsyncRoute:
        def check_readiness(self) -> object:
            return object()

    class _WrongSignatureRoute:
        async def check_readiness(self, required: object) -> object:
            return required

    route: object = (
        object()
        if kind == "absent"
        else _NonAsyncRoute()
        if kind == "non_async"
        else _WrongSignatureRoute()
    )
    evaluator = ReadinessEvaluator(
        database_probe=lambda: True,
        corpus_probe=lambda: True,
        local_vector_probe=lambda: None,
        retrieval_route_resolver=cast(Any, route),
        retrieval_profile="local_static",
        expected_retrieval_scope=LocalActiveScope(),
        provider_name="echo",
        provider_model="",
        embedding_name="fake",
        embedding_model="",
        gemini_probe=None,
        ollama_catalog_probe=None,
        budget_probe=None,
    )
    report = await evaluator.evaluate()
    assert (report.checks[1].state, report.checks[1].reason) == (
        "unknown",
        "misconfigured",
    )


@pytest.mark.parametrize("kind", ["absent", "non_async", "wrong_signature"])
async def test_gemini_probe_interface_mismatch_is_misconfigured(kind: str) -> None:
    class _NonAsyncGemini:
        def check_readiness(self) -> object:
            return object()

    class _WrongSignatureGemini:
        async def check_readiness(self, required: object) -> object:
            return required

    probe: object = (
        object()
        if kind == "absent"
        else _NonAsyncGemini()
        if kind == "non_async"
        else _WrongSignatureGemini()
    )
    evaluator, _ = _evaluator(
        provider="gemini",
        provider_model="gemini-3.8-flash",
        gemini_probe=probe,
    )
    report = await evaluator.evaluate()
    assert (report.checks[3].state, report.checks[3].reason) == (
        "unknown",
        "misconfigured",
    )
    assert (report.checks[4].state, report.checks[4].reason) == (
        "unknown",
        "misconfigured",
    )


@pytest.mark.parametrize("kind", ["absent", "non_async", "wrong_signature"])
async def test_catalog_probe_interface_mismatch_is_misconfigured(kind: str) -> None:
    class _NonAsyncCatalog:
        def check_readiness(self) -> object:
            return object()

    class _WrongSignatureCatalog:
        async def check_readiness(self, required: object) -> object:
            return required

    probe: object = (
        object()
        if kind == "absent"
        else _NonAsyncCatalog()
        if kind == "non_async"
        else _WrongSignatureCatalog()
    )
    evaluator, _ = _evaluator(
        provider="ollama",
        provider_model="generation-model",
        catalog_probe=probe,
    )
    report = await evaluator.evaluate()
    assert (report.checks[3].state, report.checks[3].reason) == (
        "unknown",
        "misconfigured",
    )
    assert (report.checks[4].state, report.checks[4].reason) == (
        "unknown",
        "misconfigured",
    )


@pytest.mark.parametrize("probe", [_GeminiProbe(TypeError()), _CatalogProbe(TypeError())])
async def test_probe_implementation_type_error_remains_unavailable(probe: object) -> None:
    if type(probe) is _GeminiProbe:
        evaluator, _ = _evaluator(
            provider="gemini",
            provider_model="gemini-3.8-flash",
            gemini_probe=probe,
        )
    else:
        evaluator, _ = _evaluator(
            provider="ollama",
            provider_model="generation-model",
            catalog_probe=probe,
        )
    report = await evaluator.evaluate()
    assert report.checks[3].reason == "unavailable"

    class _BrokenRoute:
        async def check_readiness(self) -> object:
            raise TypeError

    route_evaluator = ReadinessEvaluator(
        database_probe=lambda: True,
        corpus_probe=lambda: True,
        local_vector_probe=lambda: None,
        retrieval_route_resolver=_BrokenRoute(),
        retrieval_profile="local_static",
        expected_retrieval_scope=LocalActiveScope(),
        provider_name="echo",
        provider_model="",
        embedding_name="fake",
        embedding_model="",
        gemini_probe=None,
        ollama_catalog_probe=None,
        budget_probe=None,
    )
    route_report = await route_evaluator.evaluate()
    assert route_report.checks[1].reason == "unavailable"


async def test_gemini_runs_before_selected_ollama_embedding() -> None:
    calls: list[str] = []

    class _OrderedGemini:
        async def check_readiness(self) -> object:
            calls.append("gemini")
            return GeminiReadiness(reachable=True, model_ready=True)

    class _OrderedCatalog:
        async def check_readiness(self) -> object:
            calls.append("catalog")
            return OllamaCatalogReadiness(
                reachable=True, model_names=("embedding-model",)
            )

    evaluator, _ = _evaluator(
        provider="gemini",
        provider_model="gemini-3.8-flash",
        embedding="ollama",
        embedding_model="embedding-model",
        gemini_probe=_OrderedGemini(),
        catalog_probe=_OrderedCatalog(),
    )
    assert (await evaluator.evaluate()).ready is True
    assert calls == ["gemini", "catalog"]


async def test_gemini_cancellation_stops_selected_ollama_embedding() -> None:
    calls: list[str] = []

    class _CancelledGemini:
        async def check_readiness(self) -> object:
            calls.append("gemini")
            raise asyncio.CancelledError

    class _ForbiddenCatalog:
        async def check_readiness(self) -> object:
            calls.append("catalog")
            raise AssertionError

    evaluator, _ = _evaluator(
        provider="gemini",
        provider_model="gemini-3.8-flash",
        embedding="ollama",
        embedding_model="embedding-model",
        gemini_probe=_CancelledGemini(),
        catalog_probe=_ForbiddenCatalog(),
    )
    with pytest.raises(asyncio.CancelledError):
        await evaluator.evaluate()
    assert calls == ["gemini"]


async def test_ollama_generation_and_embedding_share_one_catalog_result() -> None:
    probe = _CatalogProbe(
        OllamaCatalogReadiness(
            reachable=True,
            model_names=("generation-model", "embedding-model"),
        )
    )
    evaluator, _ = _evaluator(
        provider="ollama",
        provider_model="generation-model",
        embedding="ollama",
        embedding_model="embedding-model",
        catalog_probe=probe,
    )
    report = await evaluator.evaluate()
    assert report.ready is True
    assert report.checks[3].state == "ready"
    assert report.checks[4].state == "ready"
    assert report.checks[5].state == "ready"
    assert probe.calls == 1


async def test_local_vector_failure_preserves_route_call_and_maps_unreachable() -> None:
    evaluator, route = _evaluator(vector_failure=True)
    report = await evaluator.evaluate()
    assert report.checks[1].model_dump() == {
        "contract_version": "1.0",
        "dimension": "vector_store",
        "state": "not_ready",
        "required": True,
        "reason": "unreachable",
    }
    assert route.calls == 1


async def test_lifecycle_uses_one_probe_for_store_and_exact_corpus() -> None:
    vector_calls: list[bool] = []
    evaluator, route = _evaluator(
        profile="lifecycle_exact", vector_calls=vector_calls
    )
    report = await evaluator.evaluate()
    assert report.checks[1].state == "ready"
    assert report.checks[6].state == "ready"
    assert route.calls == 1
    assert vector_calls == []


async def test_production_lifecycle_misconfiguration_invokes_no_resolver_hook() -> None:
    evaluator, route = _evaluator(
        profile="lifecycle_exact",
        composition_valid=False,
    )
    report = await evaluator.evaluate()
    assert report.checks[1].model_dump(exclude={"contract_version"}) == {
        "dimension": "vector_store",
        "state": "unknown",
        "required": True,
        "reason": "misconfigured",
    }
    assert report.checks[6].state == "unknown"
    assert report.checks[6].reason == "misconfigured"
    assert route.calls == 0


async def test_scope_mismatch_is_misconfigured_without_profile_switch() -> None:
    wrong_scope = ExactCorpusReference(corpus_id="other", corpus_version="v2")
    result = RetrievalProbe(
        scope=wrong_scope,
        reachable=True,
        store_ready=True,
        exact_version_ready=False,
    )
    evaluator, route = _evaluator(
        profile="firestore_static",
        route_result=result,
        expected_scope=ExactCorpusReference(
            corpus_id="public-docs", corpus_version="v1"
        ),
    )
    report = await evaluator.evaluate()
    assert (report.checks[1].state, report.checks[1].reason) == (
        "unknown",
        "misconfigured",
    )
    assert report.checks[6].state == "not_required"
    assert route.calls == 1


async def test_lifecycle_scope_mismatch_marks_store_and_exact_misconfigured() -> None:
    result = RetrievalProbe(
        scope=ExactCorpusReference(corpus_id="other", corpus_version="v2"),
        reachable=True,
        store_ready=True,
        exact_version_ready=True,
    )
    evaluator, _ = _evaluator(
        profile="lifecycle_exact",
        route_result=result,
        expected_scope=ExactCorpusReference(
            corpus_id="public-docs", corpus_version="v1"
        ),
    )
    report = await evaluator.evaluate()
    assert (report.checks[1].reason, report.checks[6].reason) == (
        "misconfigured",
        "misconfigured",
    )


async def test_invalid_one_of_two_ollama_models_does_not_block_valid_dimension() -> None:
    probe = _CatalogProbe(
        OllamaCatalogReadiness(reachable=True, model_names=("embedding-model",))
    )
    evaluator, _ = _evaluator(
        provider="ollama",
        provider_model="",
        embedding="ollama",
        embedding_model="embedding-model",
        catalog_probe=probe,
    )
    report = await evaluator.evaluate()
    assert (report.checks[3].state, report.checks[4].state) == ("unknown", "unknown")
    assert report.checks[5].state == "ready"
    assert probe.calls == 1


async def test_invalid_configured_ollama_names_make_no_catalog_request() -> None:
    probe = _CatalogProbe(AssertionError("catalog must not run"))
    evaluator, _ = _evaluator(
        provider="ollama",
        provider_model="x" * (OLLAMA_MODEL_NAME_MAX_CHARS + 1),
        catalog_probe=probe,
    )
    report = await evaluator.evaluate()
    assert report.checks[3].reason == "misconfigured"
    assert report.checks[4].reason == "misconfigured"
    assert probe.calls == 0


@pytest.mark.parametrize(
    ("catalog_result", "model", "expected"),
    [
        (OllamaCatalogReadiness(reachable=True, model_names=("Model-A",)), "model-a", "not_ready"),
        (OllamaCatalogReadiness(reachable=True, model_names=()), "missing", "not_ready"),
        ({"reachable": True, "model_names": ("model-a",)}, "model-a", "unknown"),
        (object(), "model-a", "unknown"),
    ],
)
async def test_catalog_comparison_is_exact_and_facsimiles_are_unknown(
    catalog_result: object,
    model: str,
    expected: str,
) -> None:
    evaluator, _ = _evaluator(
        provider="ollama",
        provider_model=model,
        catalog_probe=_CatalogProbe(catalog_result),
    )
    report = await evaluator.evaluate()
    assert report.checks[4].state == expected


async def test_configured_ollama_model_name_exact_n_is_ready() -> None:
    name = "x" * OLLAMA_MODEL_NAME_MAX_CHARS
    evaluator, _ = _evaluator(
        provider="ollama",
        provider_model=name,
        catalog_probe=_CatalogProbe(
            OllamaCatalogReadiness(reachable=True, model_names=(name,))
        ),
    )
    assert (await evaluator.evaluate()).checks[4].state == "ready"


async def test_cancellation_propagates_and_stops_later_work() -> None:
    evaluator, route = _evaluator(
        provider="ollama",
        provider_model="generation-model",
        catalog_probe=_CatalogProbe(asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await evaluator.evaluate()
    assert route.calls == 1


async def test_route_cancellation_stops_corpus_and_provider_probes() -> None:
    calls: list[str] = []

    class _CancelledRoute:
        async def check_readiness(self) -> object:
            calls.append("route")
            raise asyncio.CancelledError

    class _ForbiddenCatalog:
        async def check_readiness(self) -> object:
            calls.append("catalog")
            raise AssertionError

    def database_probe() -> bool:
        calls.append("database")
        return True

    def corpus_probe() -> bool:
        calls.append("corpus")
        return True

    evaluator = ReadinessEvaluator(
        database_probe=database_probe,
        corpus_probe=corpus_probe,
        local_vector_probe=lambda: calls.append("vector"),
        retrieval_route_resolver=_CancelledRoute(),
        retrieval_profile="local_static",
        expected_retrieval_scope=LocalActiveScope(),
        provider_name="ollama",
        provider_model="model-a",
        embedding_name="fake",
        embedding_model="",
        gemini_probe=None,
        ollama_catalog_probe=_ForbiddenCatalog(),
        budget_probe=None,
    )
    with pytest.raises(asyncio.CancelledError):
        await evaluator.evaluate()
    assert calls == ["database", "route"]


def _catalog_transport(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    captured: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(status, content=body, headers=headers, request=request)

    return httpx.MockTransport(handler)


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.delivered = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.delivered += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _OneByteStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.delivered = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index in range(len(self.body)):
            self.delivered += 1
            yield self.body[index : index + 1]


def _stream_transport(
    stream: httpx.AsyncByteStream,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            headers=headers,
            stream=stream,
            request=request,
        )
    )


@pytest.mark.parametrize(
    "outcome",
    ["success", "negative", "error", "timeout", "cancellation"],
)
async def test_ollama_catalog_emits_no_endpoint_or_model_logs(
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint_canary = "private-readiness-endpoint.invalid"
    model_canary = "PRIVATE-READINESS-MODEL-CANARY"

    def handler(request: httpx.Request) -> httpx.Response:
        if outcome == "error":
            raise RuntimeError(f"{endpoint_canary}:{model_canary}")
        if outcome == "timeout":
            raise httpx.ReadTimeout(endpoint_canary, request=request)
        if outcome == "cancellation":
            raise asyncio.CancelledError
        status = 503 if outcome == "negative" else 200
        return httpx.Response(
            status,
            content=json.dumps({"models": [{"name": model_canary}]}).encode(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = OllamaCatalogProbe(
        base_url=f"http://{endpoint_canary}:11434",
        client=client,
        owns_client=False,
    )
    caplog.set_level("DEBUG")
    caught: BaseException | None = None
    try:
        if outcome == "success":
            assert (await probe.check_readiness()).model_names == (model_canary,)
        elif outcome == "timeout":
            assert await probe.check_readiness() == OllamaCatalogReadiness(
                reachable=False,
                model_names=(),
            )
        elif outcome == "cancellation":
            with pytest.raises(asyncio.CancelledError) as cancellation_error:
                await probe.check_readiness()
            caught = cancellation_error.value
        else:
            with pytest.raises(ReadinessError) as readiness_error:
                await probe.check_readiness()
            caught = readiness_error.value
    finally:
        await probe.aclose()
        await client.aclose()

    assert caplog.records == []
    if caught is not None:
        _assert_content_free(caught, endpoint_canary)
        _assert_content_free(caught, model_canary)


async def test_application_owned_real_transport_emits_no_httpcore_trace_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint_canary = "private-httpcore-endpoint.invalid"
    model_canary = "PRIVATE-HTTPCORE-MODEL-CANARY"
    body = json.dumps({"models": [{"name": model_canary}]}).encode()
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    probe = OllamaCatalogProbe.application_owned(
        base_url=f"http://{endpoint_canary}:11434"
    )
    client = cast(httpx.AsyncClient, object.__getattribute__(probe, "_client"))
    transport = cast(Any, object.__getattribute__(client, "_transport"))
    transport._pool._network_backend = httpcore.AsyncMockBackend([response])

    caplog.set_level("DEBUG")
    try:
        assert (await probe.check_readiness()).model_names == (model_canary,)
    finally:
        await probe.aclose()

    assert caplog.records == []


async def test_overlapping_catalog_suppression_preserves_unrelated_task_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    arrivals = 0
    both_entered = asyncio.Event()
    unrelated_logged = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_entered.set()
        await release.wait()
        return httpx.Response(200, json={"models": []}, request=request)

    async def unrelated() -> None:
        await both_entered.wait()
        logging.getLogger("httpx").info("UNRELATED-TRANSPORT-LOG")
        unrelated_logged.set()

    caplog.set_level("DEBUG")
    unrelated_task = asyncio.create_task(unrelated())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probes = tuple(
        OllamaCatalogProbe(
            base_url="http://private-overlap.invalid:11434",
            client=client,
            owns_client=False,
        )
        for _ in range(2)
    )
    readiness_tasks = tuple(
        asyncio.create_task(probe.check_readiness()) for probe in probes
    )
    await asyncio.wait_for(unrelated_logged.wait(), timeout=1)
    release.set()
    await asyncio.gather(*readiness_tasks, unrelated_task)
    logging.getLogger("httpcore.connection").debug("AFTER-TRANSPORT-LOG")
    await client.aclose()

    transport_records = [
        record
        for record in caplog.records
        if record.name == "httpx" or record.name.startswith("httpcore.")
    ]
    assert [(record.name, record.getMessage()) for record in transport_records] == [
        ("httpx", "UNRELATED-TRANSPORT-LOG"),
        ("httpcore.connection", "AFTER-TRANSPORT-LOG"),
    ]


async def test_ollama_catalog_exact_request_and_shape() -> None:
    captured: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=_catalog_transport(
            json.dumps({"models": [{"name": "model-a"}]}).encode(),
            captured=captured,
        )
    )
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434/",
        client=client,
        owns_client=False,
    )
    result = await probe.check_readiness()
    assert result == OllamaCatalogReadiness(reachable=True, model_names=("model-a",))
    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert str(captured[0].url) == "http://ollama:11434/api/tags"
    assert captured[0].content == b""
    await probe.aclose()
    assert not client.is_closed
    await client.aclose()


async def test_ollama_catalog_request_ignores_client_and_url_credentials() -> None:
    captured: list[httpx.Request] = []
    hook_calls = 0

    async def forbidden_hook(request: httpx.Request) -> None:
        nonlocal hook_calls
        del request
        hook_calls += 1

    client = httpx.AsyncClient(
        transport=_catalog_transport(b'{"models":[]}', captured=captured),
        headers={
            "Authorization": "Bearer PRIVATE-CLIENT-AUTH",
            "Cookie": "PRIVATE-CLIENT-COOKIE=1",
            "X-Private": "PRIVATE-CLIENT-HEADER",
        },
        cookies={"PRIVATE-COOKIE-JAR": "secret"},
        auth=httpx.BasicAuth("PRIVATE-AUTH-USER", "PRIVATE-AUTH-PASSWORD"),
        event_hooks={"request": [forbidden_hook]},
    )
    probe = OllamaCatalogProbe(
        base_url="http://PRIVATE-URL-USER:PRIVATE-URL-PASSWORD@ollama:11434",
        client=client,
        owns_client=False,
    )
    assert (await probe.check_readiness()).reachable is True
    assert hook_calls == 0
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "http://ollama:11434/api/tags"
    assert set(request.headers) == {"host"}
    assert request.content == b""
    await probe.aclose()
    await client.aclose()


async def test_ollama_catalog_non_2xx_is_non_authoritative() -> None:
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(
            transport=_catalog_transport(b"PRIVATE-NON-2XX", status=503)
        ),
        owns_client=True,
    )
    with pytest.raises(ReadinessError) as caught:
        await probe.check_readiness()
    _assert_content_free(caught.value, "PRIVATE-NON-2XX")
    await probe.aclose()


@pytest.mark.parametrize(
    ("headers", "succeeds"),
    [
        ({}, True),
        ({"content-length": "not-a-number"}, False),
        ({"content-length": "-1"}, False),
        ({"content-length": str(OLLAMA_CATALOG_MAX_RESPONSE_BYTES + 1)}, False),
        ({"content-length": "1"}, True),
    ],
)
async def test_ollama_catalog_content_length_is_only_an_early_bound(
    headers: dict[str, str], succeeds: bool
) -> None:
    body = b'{"models":[]}'
    stream = _ChunkedStream(tuple(body[index : index + 1] for index in range(len(body))))
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(
            transport=_stream_transport(stream, headers=headers)
        ),
        owns_client=True,
    )
    if succeeds:
        assert (await probe.check_readiness()).reachable is True
        assert stream.delivered == len(body)
    else:
        with pytest.raises(ReadinessError):
            await probe.check_readiness()
        assert stream.delivered == 0
    await probe.aclose()


@pytest.mark.parametrize(
    "size",
    [OLLAMA_CATALOG_MAX_RESPONSE_BYTES, OLLAMA_CATALOG_MAX_RESPONSE_BYTES + 1],
)
async def test_ollama_catalog_exact_response_byte_bound(size: int) -> None:
    prefix = b'{"models":[]}'
    body = prefix + b" " * (size - len(prefix))
    chunks = (body[:17], body[17:1000], body[1000:])
    stream = _ChunkedStream(chunks)
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(transport=_stream_transport(stream)),
        owns_client=True,
    )
    if size == OLLAMA_CATALOG_MAX_RESPONSE_BYTES:
        assert (await probe.check_readiness()).reachable is True
        assert stream.delivered == size
    else:
        with pytest.raises(ReadinessError):
            await probe.check_readiness()
        assert stream.delivered == size
    await probe.aclose()


async def test_ollama_catalog_one_byte_fragmentation_enforces_n_plus_one() -> None:
    prefix = b'{"models":[]}'
    body = prefix + b" " * (OLLAMA_CATALOG_MAX_RESPONSE_BYTES + 1 - len(prefix))
    stream = _OneByteStream(body)
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(
            transport=_stream_transport(cast(Any, stream), headers={"content-length": "1"})
        ),
        owns_client=True,
    )
    with pytest.raises(ReadinessError):
        await probe.check_readiness()
    assert stream.delivered == OLLAMA_CATALOG_MAX_RESPONSE_BYTES + 1
    await probe.aclose()


async def test_ollama_response_model_name_exact_n_is_accepted() -> None:
    name = "x" * OLLAMA_MODEL_NAME_MAX_CHARS
    body = json.dumps({"models": [{"name": name}]}).encode()
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(transport=_catalog_transport(body)),
        owns_client=True,
    )
    assert (await probe.check_readiness()).model_names == (name,)
    await probe.aclose()


async def test_application_owned_catalog_client_is_hardened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []
    original = httpx.AsyncClient.__init__

    def capture(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        observed.append(dict(kwargs))
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", capture)
    probe = OllamaCatalogProbe.application_owned(base_url="http://ollama:11434")
    assert observed == [
        {"timeout": None, "follow_redirects": False, "trust_env": False}
    ]
    await probe.aclose()


async def test_catalog_close_failure_is_content_free_and_attempted_once() -> None:
    canary = "PRIVATE-CLOSE-CANARY"

    class _BrokenTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.close_calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            del request
            raise AssertionError

        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError(canary)

    transport = _BrokenTransport()
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(transport=transport),
        owns_client=True,
    )
    with pytest.raises(ReadinessError) as caught:
        await probe.aclose()
    _assert_content_free(caught.value, canary)
    await probe.aclose()
    assert transport.close_calls == 1


async def test_catalog_normalizes_mutated_readiness_error_from_transport() -> None:
    canary = "PRIVATE-INJECTED-READINESS-ERROR-CANARY"
    injected = ReadinessError()
    injected.args = (canary,)
    object.__setattr__(injected, "hidden", canary)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise injected

    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        owns_client=True,
    )
    with pytest.raises(ReadinessError) as caught:
        await probe.check_readiness()
    assert caught.value is not injected
    _assert_content_free(caught.value, canary)
    await probe.aclose()


class _CloseTrackedCatalog:
    def __init__(self) -> None:
        self.readiness_calls = 0
        self.close_calls = 0

    async def check_readiness(self) -> object:
        self.readiness_calls += 1
        return OllamaCatalogReadiness(
            reachable=True,
            model_names=("generation-model",),
        )

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize("startup_fails", [False, True])
def test_app_owned_catalog_closes_once_and_borrowed_catalog_never_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    startup_fails: bool,
) -> None:
    owned = _CloseTrackedCatalog()
    monkeypatch.setattr(
        "app.main.OllamaCatalogProbe.application_owned",
        lambda *, base_url: owned,
    )
    corpus_path = None
    if startup_fails:
        corpus_path = tmp_path / "empty-corpus"
        corpus_path.mkdir()
    settings = Settings(
        provider="ollama",
        ollama_model="generation-model",
        database_path=tmp_path / "owned.db",
        chroma_path=tmp_path / "owned-chroma",
        corpus_path=corpus_path,
    )
    application = create_app(settings, provider=EchoProvider())
    if startup_fails:
        with pytest.raises(CorpusStartupError):
            with TestClient(application):
                pass
    else:
        with TestClient(application) as client:
            assert client.get("/healthz").content == b'{"status":"ok"}'
    assert owned.close_calls == 1

    borrowed = _CloseTrackedCatalog()
    borrowed_app = create_app(
        Settings(
            provider="ollama",
            ollama_model="generation-model",
            database_path=tmp_path / "borrowed.db",
            chroma_path=tmp_path / "borrowed-chroma",
        ),
        provider=EchoProvider(),
        ollama_catalog_probe=borrowed,
    )
    with TestClient(borrowed_app) as client:
        assert client.get("/healthz").status_code == 200
    assert borrowed.close_calls == 0


def test_healthz_does_not_invoke_readiness_and_readyz_hides_provider_failure(
    tmp_path: Any,
) -> None:
    catalog = _CloseTrackedCatalog()
    application = create_app(
        Settings(
            provider="ollama",
            ollama_model="missing-model",
            database_path=tmp_path / "test.db",
            chroma_path=tmp_path / "chroma",
        ),
        provider=EchoProvider(),
        ollama_catalog_probe=catalog,
    )
    with TestClient(application) as client:
        assert client.get("/healthz").content == b'{"status":"ok"}'
        assert catalog.readiness_calls == 0
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.content == (
            b'{"status":"not_ready","checks":{"database":true,'
            b'"vector_store":true,"corpus":true}}'
        )
        assert catalog.readiness_calls == 1


async def test_catalog_timeout_maps_to_unreachable_and_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Timeout:
        async def __aenter__(self) -> None:
            raise TimeoutError

        async def __aexit__(self, *args: object) -> None:
            del args

    observed: list[float] = []

    def timeout(seconds: float) -> _Timeout:
        observed.append(seconds)
        return _Timeout()

    monkeypatch.setattr("app.readiness.asyncio.timeout", timeout)
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(transport=_catalog_transport(b'{"models":[]}')),
        owns_client=True,
    )
    assert await probe.check_readiness() == OllamaCatalogReadiness(
        reachable=False, model_names=()
    )
    assert observed == [5.0]
    await probe.aclose()


@pytest.mark.parametrize("stage", ["request", "body", "response_close"])
async def test_catalog_cancellation_propagates_from_every_transport_window(
    stage: str,
) -> None:
    class _CancelStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            if stage == "body":
                raise asyncio.CancelledError
            yield b'{"models":[]}'

        async def aclose(self) -> None:
            if stage == "response_close":
                raise asyncio.CancelledError

    def handler(request: httpx.Request) -> httpx.Response:
        if stage == "request":
            raise asyncio.CancelledError
        return httpx.Response(200, stream=_CancelStream(), request=request)

    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        owns_client=False,
    )
    with pytest.raises(asyncio.CancelledError):
        await probe.check_readiness()
    await probe.aclose()


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"models": [{"name": "x" * (OLLAMA_MODEL_NAME_MAX_CHARS + 1)}]}).encode(),
        json.dumps({"models": [{"name": "same"}, {"name": "same"}]}).encode(),
        json.dumps({"models": [{}]}).encode(),
        json.dumps({"models": []}).encode() + b"x" * OLLAMA_CATALOG_MAX_RESPONSE_BYTES,
    ],
)
async def test_ollama_catalog_malformed_or_oversized_is_content_free(body: bytes) -> None:
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=httpx.AsyncClient(transport=_catalog_transport(body)),
        owns_client=True,
    )
    with pytest.raises(ReadinessError) as caught:
        await probe.check_readiness()
    assert "same" not in str(caught.value)
    await probe.aclose()


async def test_malformed_catalog_error_hides_response_and_endpoint_traceback() -> None:
    endpoint_canary = "private-malformed-endpoint.invalid"
    body_canary = b"PRIVATE-MALFORMED-RESPONSE-CANARY"
    probe = OllamaCatalogProbe(
        base_url=f"http://{endpoint_canary}:11434",
        client=httpx.AsyncClient(transport=_catalog_transport(body_canary)),
        owns_client=True,
    )
    with pytest.raises(ReadinessError) as caught:
        await probe.check_readiness()
    _assert_content_free(caught.value, endpoint_canary, body_canary.decode())
    assert caught.value.__traceback__ is None
    await probe.aclose()


@pytest.mark.parametrize(
    "outcome", ["success", "transport", "error", "malformed"]
)
async def test_catalog_transient_response_and_error_release_without_gc(
    outcome: str,
) -> None:
    response_ref: weakref.ReferenceType[httpx.Response] | None = None
    error_ref: weakref.ReferenceType[BaseException] | None = None

    class _TransportFailure(httpx.TransportError):
        pass

    class _RawFailure(RuntimeError):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_ref, error_ref
        if outcome in {"transport", "error"}:
            error = (
                _TransportFailure("PRIVATE-TRANSIENT-ERROR", request=request)
                if outcome == "transport"
                else _RawFailure("PRIVATE-TRANSIENT-ERROR")
            )
            error_ref = weakref.ref(error)
            raise error
        response = httpx.Response(
            200,
            content=(
                b'{"models":[]}'
                if outcome == "success"
                else b"PRIVATE-TRANSIENT-RESPONSE"
            ),
            request=request,
        )
        response_ref = weakref.ref(response)
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434",
        client=client,
        owns_client=False,
    )
    if outcome == "success":
        assert (await probe.check_readiness()).reachable is True
    elif outcome == "transport":
        assert (await probe.check_readiness()).reachable is False
    else:
        with pytest.raises(ReadinessError) as caught:
            await probe.check_readiness()
        assert caught.value.__traceback__ is None
    assert response_ref is None or response_ref() is None
    assert error_ref is None or error_ref() is None
    await client.aclose()


async def test_malformed_decoded_catalog_releases_partial_objects_without_gc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DecodedCanary:
        pass

    canary = _DecodedCanary()
    canary_ref = weakref.ref(canary)
    payloads: list[object] = [
        {"models": [{"name": "valid"}, {"name": canary}]}
    ]
    del canary

    def loads(raw: object) -> object:
        del raw
        return payloads.pop()

    monkeypatch.setattr("app.readiness.json.loads", loads)
    client = httpx.AsyncClient(
        transport=_catalog_transport(b'{"models":[]}')
    )
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434", client=client, owns_client=False
    )
    with pytest.raises(ReadinessError) as caught:
        await probe.check_readiness()
    assert caught.value.__traceback__ is None
    assert payloads == []
    assert canary_ref() is None
    await client.aclose()


async def test_preclosed_borrowed_catalog_client_performs_no_transport_io() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"models": []}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = OllamaCatalogProbe(
        base_url="http://ollama:11434", client=client, owns_client=False
    )
    await client.aclose()
    with pytest.raises(ReadinessError):
        await probe.check_readiness()
    evaluator, _ = _evaluator(
        provider="ollama", provider_model="model-a", catalog_probe=probe
    )
    report = await evaluator.evaluate()
    assert (report.checks[3].state, report.checks[3].reason) == (
        "unknown",
        "unavailable",
    )
    assert calls == 0
    await probe.aclose()
    assert client.is_closed


async def test_body_cancellation_skips_blocked_response_close_and_stops_evaluation() -> None:
    cancellation = asyncio.CancelledError()
    requests = 0
    close_calls = 0
    close_release = asyncio.Event()

    class CancelThenBlockClose(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise cancellation
            yield b""

        async def aclose(self) -> None:
            nonlocal close_calls
            close_calls += 1
            await close_release.wait()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, stream=CancelThenBlockClose(), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = OllamaCatalogProbe(
        base_url="http://private-cancel.invalid:11434",
        client=client,
        owns_client=False,
    )
    evaluator, route = _evaluator(
        provider="ollama",
        provider_model="generation-model",
        embedding="ollama",
        embedding_model="embedding-model",
        catalog_probe=probe,
    )
    task = asyncio.create_task(evaluator.evaluate())
    completed, pending = await asyncio.wait({task}, timeout=1)
    if pending:
        close_release.set()
        await asyncio.gather(*pending, return_exceptions=True)
    assert completed == {task}
    assert pending == set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value is cancellation
    assert requests == 1
    assert close_calls == 0
    assert not close_release.is_set()
    assert route.calls == 1
    await client.aclose()


async def test_ollama_catalog_model_count_bound() -> None:
    valid = json.dumps(
        {"models": [{"name": f"m{index}"} for index in range(OLLAMA_CATALOG_MAX_MODELS)]}
    ).encode()
    invalid = json.dumps(
        {"models": [{"name": f"m{index}"} for index in range(OLLAMA_CATALOG_MAX_MODELS + 1)]}
    ).encode()
    for body, succeeds in ((valid, True), (invalid, False)):
        probe = OllamaCatalogProbe(
            base_url="http://ollama:11434",
            client=httpx.AsyncClient(transport=_catalog_transport(body)),
            owns_client=True,
        )
        if succeeds:
            assert len((await probe.check_readiness()).model_names) == OLLAMA_CATALOG_MAX_MODELS
        else:
            with pytest.raises(ReadinessError):
                await probe.check_readiness()
        await probe.aclose()


async def test_budget_is_always_not_required_without_policy_authority() -> None:
    class _ForbiddenBudget:
        async def check_readiness(self) -> object:
            raise AssertionError("future budget authority must remain inert")

    evaluator, _ = _evaluator(budget_probe=_ForbiddenBudget())
    report = await evaluator.evaluate()
    assert report.checks[7].model_dump() == {
        "contract_version": "1.0",
        "dimension": "budget",
        "state": "not_required",
        "required": False,
        "reason": "not_required",
    }


async def test_readiness_emits_no_logs_and_performs_no_unselected_external_work(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("external work is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.stream", forbidden)
    monkeypatch.setattr("app.providers.gemini.GeminiProvider.stream", forbidden)
    caplog.set_level("DEBUG")
    evaluator, _ = _evaluator()
    assert (await evaluator.evaluate()).ready is True
    assert caplog.records == []
