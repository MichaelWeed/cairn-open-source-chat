import math

import pytest
from pydantic import ValidationError

from app.api.contracts import MESSAGE_MAX_CHARS, RETRIEVED_CONTEXT_MAX_CHARS
from app.ingest.provenance import MAX_CITATION_TITLE_CHARS, MAX_MANIFEST_BYTES
from app.retrieval_contracts import (
    MAX_RETRIEVAL_RESULTS,
    RETRIEVAL_CONTRACT_VERSION,
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalError,
    RetrievalProbe,
    RetrievalRequest,
    RetrievalResult,
    RetrievedChunk,
)


def _chunk(**overrides: object) -> RetrievedChunk:
    values: dict[str, object] = {
        "chunk_id": "doc-1::chunk::0",
        "document_id": "doc-1",
        "source": "faq.md",
        "chunk_index": 0,
        "text": "Thirty days.",
        "distance": 1.2,
    }
    values.update(overrides)
    return RetrievedChunk.model_validate(values)


def _request(**overrides: object) -> RetrievalRequest:
    values: dict[str, object] = {
        "contract_version": RETRIEVAL_CONTRACT_VERSION,
        "scope": {"kind": "local_active", "local_corpus_compatibility": "2"},
        "query": " returns? ",
        "max_results": 4,
        "max_distance": 1.2,
        "distance_measure": "squared_l2",
    }
    values.update(overrides)
    return RetrievalRequest.model_validate(values)


def test_authoritative_limits_are_imported_and_contract_models_are_frozen() -> None:
    assert MESSAGE_MAX_CHARS == 500
    assert RETRIEVED_CONTEXT_MAX_CHARS == 12_000
    assert MAX_CITATION_TITLE_CHARS == 160
    assert MAX_MANIFEST_BYTES == 1_048_576
    assert MAX_RETRIEVAL_RESULTS == 6
    with pytest.raises(ValidationError):
        _request().query = "changed"


@pytest.mark.parametrize("query", ["", " \n\t ", "bad\x00query", "x" * 501])
def test_request_rejects_invalid_query_without_normalizing(query: str) -> None:
    with pytest.raises(ValidationError):
        _request(query=query)


def test_request_preserves_exact_unicode_query_at_public_boundary() -> None:
    query = " 🧭" * 250
    request = _request(query=query)
    assert len(request.query) == MESSAGE_MAX_CHARS
    assert request.query == query


@pytest.mark.parametrize("value", [0, 7, True, 1.5, "4"])
def test_request_rejects_invalid_max_results(value: object) -> None:
    with pytest.raises(ValidationError):
        _request(max_results=value)


@pytest.mark.parametrize("value", [-0.1, math.nan, math.inf, -math.inf, True, "1.2"])
def test_request_rejects_invalid_distance(value: object) -> None:
    with pytest.raises(ValidationError):
        _request(max_distance=value)


@pytest.mark.parametrize(
    ("scope", "valid"),
    [
        ({"kind": "local_active", "local_corpus_compatibility": "2"}, True),
        ({"kind": "local_active", "local_corpus_compatibility": "1"}, False),
        ({"kind": "exact", "corpus_id": "support-docs", "corpus_version": "2026.09_1"}, True),
        ({"kind": "exact", "corpus_id": "Support_Docs", "corpus_version": "v1"}, False),
        ({"kind": "exact", "corpus_id": "latest", "corpus_version": "v1"}, False),
        ({"kind": "exact", "corpus_id": "docs", "corpus_version": "current"}, False),
        ({"kind": "exact", "corpus_id": "docs", "corpus_version": ".v1"}, False),
        ({"kind": "exact", "corpus_id": "docs", "corpus_version": "active"}, False),
    ],
)
def test_scope_grammar_and_moving_aliases(scope: dict[str, object], valid: bool) -> None:
    if valid:
        assert _request(scope=scope).scope.kind in {"local_active", "exact"}
    else:
        with pytest.raises(ValidationError):
            _request(scope=scope)


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_id": ""},
        {"chunk_id": "c" * 8193},
        {"document_id": "d" * 4097},
        {"source": "bad\x7fsource"},
        {"chunk_index": True},
        {"chunk_index": -1},
        {"text": ""},
        {"text": "x" * 3001},
        {"distance": math.nan},
        {"distance": True},
        {"citation_title": "title"},
        {"citation_url": "https://example.com"},
        {"citation_title": "x" * 161, "citation_url": "https://example.com"},
        {"citation_title": "Title", "citation_url": "https://user@example.com/private"},
        {"citation_title": "Title", "citation_url": "ftp://example.com/file"},
        {"citation_title": "Title", "citation_url": "https://example.com/white space"},
    ],
)
def test_chunk_rejects_hostile_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _chunk(**overrides)


def test_chunk_preserves_whitespace_text_and_full_http_url() -> None:
    text = "x" + " " * 2999
    url = "http://example.com/docs?q=a#part"
    chunk = _chunk(text=text, citation_title=" Returns ", citation_url=url)
    assert chunk.text == text
    assert chunk.citation_title == " Returns "
    assert chunk.citation_url == url


def test_citation_url_accepts_https_at_boundary_and_rejects_overflow() -> None:
    prefix = "https://example.com/"
    boundary = prefix + "x" * (MAX_MANIFEST_BYTES - len(prefix))
    assert _chunk(citation_title="Title", citation_url=boundary).citation_url == boundary
    with pytest.raises(ValidationError):
        _chunk(citation_title="Title", citation_url=boundary + "x")


def test_result_derives_threshold_semantics_and_rejects_duplicate_identity() -> None:
    scope = LocalActiveScope()
    equal = RetrievalResult(
        scope=scope, distance_measure="squared_l2", max_distance=1.2, chunks=(_chunk(),)
    )
    assert equal.best_distance == 1.2
    assert equal.refused is False
    empty = RetrievalResult(scope=scope, distance_measure="squared_l2", max_distance=1.2, chunks=())
    assert empty.best_distance is None
    assert empty.refused is True
    with pytest.raises(ValidationError):
        RetrievalResult(
            scope=scope,
            distance_measure="squared_l2",
            max_distance=1.2,
            chunks=(_chunk(), _chunk()),
        )


def test_result_requires_repeated_document_metadata_identity() -> None:
    with pytest.raises(ValidationError):
        RetrievalResult(
            scope=LocalActiveScope(),
            distance_measure="squared_l2",
            max_distance=1.2,
            chunks=(
                _chunk(),
                _chunk(
                    chunk_id="doc-1::chunk::1",
                    chunk_index=1,
                    source="other.md",
                ),
            ),
        )


def test_probe_fail_closed_readiness_invariants() -> None:
    scope = LocalActiveScope()
    with pytest.raises(ValidationError):
        RetrievalProbe(scope=scope, reachable=False, store_ready=True, exact_version_ready=False)
    with pytest.raises(ValidationError):
        RetrievalProbe(scope=scope, reachable=True, store_ready=True, exact_version_ready=True)
    exact = ExactCorpusReference(corpus_id="docs", corpus_version="v1")
    assert RetrievalProbe(
        scope=exact, reachable=True, store_ready=True, exact_version_ready=True
    ).exact_version_ready


def test_retrieval_errors_expose_only_allowlisted_code_and_fixed_message() -> None:
    secret = "caller-secret"
    error = RetrievalError("store_unavailable")
    assert error.code == "store_unavailable"
    assert secret not in str(error)
    assert secret not in repr(error)
    with pytest.raises(ValueError, match="unsupported retrieval error code"):
        RetrievalError(secret)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported retrieval error code"):
        RetrievalError([secret])  # type: ignore[arg-type]


def test_unknown_fields_are_rejected_at_every_contract_level() -> None:
    with pytest.raises(ValidationError):
        _request(unknown=True)
    with pytest.raises(ValidationError):
        LocalActiveScope.model_validate(
            {"kind": "local_active", "local_corpus_compatibility": "2", "unknown": True}
        )
    with pytest.raises(ValidationError):
        _chunk(unknown=True)
    with pytest.raises(ValidationError):
        RetrievalResult.model_validate(
            {
                "scope": {"kind": "local_active", "local_corpus_compatibility": "2"},
                "distance_measure": "squared_l2",
                "max_distance": 1.2,
                "chunks": [],
                "unknown": True,
            }
        )
    with pytest.raises(ValidationError):
        RetrievalProbe.model_validate(
            {
                "scope": {"kind": "local_active", "local_corpus_compatibility": "2"},
                "reachable": True,
                "store_ready": True,
                "exact_version_ready": False,
                "unknown": True,
            }
        )
