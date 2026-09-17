import hashlib
import json
from collections.abc import Sequence
from datetime import date

import pytest
from pydantic import ValidationError

from app.ingest.manifest_workflow import (
    ManifestDocumentApproval,
    ManifestReviewPolicy,
    ManifestWorkflowError,
    OriginAuthorityClaim,
    ReviewedManifestSnapshot,
    approve_manifest_draft,
    generate_manifest_draft,
    plan_manifest_changes,
    plan_reviewed_candidate,
    validate_reviewed_manifest,
)
from app.ingest.planner import (
    CandidateDocumentSnapshot,
    CandidateSourceSnapshot,
    EmbeddingSpecification,
)
from app.ingest.provenance import CorpusProvenanceError, parse_provenance_manifest
from app.retrieval_contracts import ExactCorpusReference


def _policy(**changes: object) -> ManifestReviewPolicy:
    values: dict[str, object] = {
        "evaluation_date": date(2026, 9, 17),
        "max_review_age_days": 30,
        "approved_authorities": ("Documentation team",),
        "claims": (
            OriginAuthorityClaim(origin="https://docs.example.com", authority="Documentation team"),
        ),
    }
    values.update(changes)
    return ManifestReviewPolicy.model_validate(values)


def _source(
    documents: dict[str, bytes] | None = None, *, entry_changes: dict[str, object] | None = None
) -> CandidateSourceSnapshot:
    documents = documents or {"guide.md": b"Reviewed guide"}
    changes = entry_changes or {}
    entries = {
        path: {
            "title": f"Title for {path}",
            "url": f"https://docs.example.com/{path}",
            "sha256": hashlib.sha256(content).hexdigest(),
            "owner": "Documentation team",
            "reviewed_at": "2026-09-08",
            "public": True,
            **changes,
        }
        for path, content in documents.items()
    }
    return CandidateSourceSnapshot(
        manifest_bytes=json.dumps({"version": 1, "documents": entries}).encode(),
        documents=tuple(
            CandidateDocumentSnapshot(relative_path=path, content=content)
            for path, content in documents.items()
        ),
    )


def _approval(**changes: object) -> ManifestDocumentApproval:
    values: dict[str, object] = {
        "title": "Reviewed guide",
        "url": "https://docs.example.com/guide",
        "authority": "Documentation team",
        "reviewed_at": date(2026, 9, 17),
        "approved": True,
    }
    values.update(changes)
    return ManifestDocumentApproval.model_validate(values)


def test_reviewed_snapshot_is_format_independent_and_binds_policy() -> None:
    source = _source()
    formatted = source.model_copy(
        update={"manifest_bytes": json.dumps(json.loads(source.manifest_bytes), indent=4).encode()}
    )
    first = validate_reviewed_manifest(source, _policy())
    second = validate_reviewed_manifest(formatted, _policy())
    changed_policy = validate_reviewed_manifest(source, _policy(max_review_age_days=31))

    assert first == second
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.snapshot_sha256 != changed_policy.snapshot_sha256
    assert first.policy_sha256 != changed_policy.policy_sha256
    with pytest.raises(ValidationError):
        first.snapshot_sha256 = "0" * 64
    with pytest.raises(ManifestWorkflowError):
        first.model_copy(update={"snapshot_sha256": "0" * 64})
    assert ReviewedManifestSnapshot.model_validate_json(first.model_dump_json()) == first


@pytest.mark.parametrize(
    ("entry_changes", "policy_changes", "code"),
    (
        ({"reviewed_at": "2026-09-18"}, {}, "review_out_of_window"),
        ({"reviewed_at": "2026-08-17"}, {}, "review_out_of_window"),
        ({"owner": "Unknown team"}, {}, "unapproved_source"),
        ({"url": "https://private.example.com/guide"}, {}, "disallowed_origin"),
        ({"url": "https://DOCS.example.com/guide"}, {}, "disallowed_origin"),
        (
            {},
            {
                "claims": (
                    OriginAuthorityClaim(
                        origin="https://docs.example.com", authority="Security team"
                    ),
                ),
                "approved_authorities": ("Documentation team", "Security team"),
            },
            "claim_mismatch",
        ),
    ),
)
def test_review_policy_rejections_are_normalized(
    entry_changes: dict[str, object], policy_changes: dict[str, object], code: str
) -> None:
    secret = "not-present-in-errors"
    source = _source(entry_changes={**entry_changes, "title": secret})
    with pytest.raises(ManifestWorkflowError) as caught:
        validate_reviewed_manifest(source, _policy(**policy_changes))
    rendered = str(caught.value) + repr(caught.value) + caught.value.json()
    assert caught.value.code == code
    assert secret not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_policy_rejects_unapproved_and_conflicting_claim_authority() -> None:
    with pytest.raises(ManifestWorkflowError) as unapproved:
        validate_reviewed_manifest(
            _source(),
            _policy(
                claims=(
                    OriginAuthorityClaim(
                        origin="https://docs.example.com", authority="Security team"
                    ),
                )
            ),
        )
    assert unapproved.value.code == "invalid_policy"

    with pytest.raises(ManifestWorkflowError) as conflict:
        validate_reviewed_manifest(
            _source(),
            _policy(
                approved_authorities=("Documentation team", "Security team"),
                claims=(
                    OriginAuthorityClaim(
                        origin="https://docs.example.com", authority="Documentation team"
                    ),
                    OriginAuthorityClaim(
                        origin="https://docs.example.com", authority="Security team"
                    ),
                ),
            ),
        )
    assert conflict.value.code == "conflicting_claim"


def test_policy_requires_canonical_exact_origins() -> None:
    for origin in (
        "https://docs.example.com/",
        "https://DOCS.example.com",
        "https://docs.example.com:443",
        "https://user@docs.example.com",
    ):
        with pytest.raises(ManifestWorkflowError) as caught:
            OriginAuthorityClaim(origin=origin, authority="Documentation team")
        assert caught.value.code == "invalid_model"


def test_dry_run_is_sorted_content_free_and_deterministic() -> None:
    before = validate_reviewed_manifest(_source({"z.md": b"old", "middle.md": b"same"}), _policy())
    after = validate_reviewed_manifest(
        _source({"a.md": b"new", "middle.md": b"changed", "b.md": b"new too"}),
        _policy(),
    )
    first = plan_manifest_changes(before, after)
    second = plan_manifest_changes(before, after)

    assert first == second
    assert [item.relative_path for item in first.create] == ["a.md", "b.md"]
    assert [item.relative_path for item in first.update] == ["middle.md"]
    assert [item.relative_path for item in first.remove] == ["z.md"]
    assert "Reviewed" not in first.model_dump_json()


def test_draft_is_deterministic_contains_hashes_and_is_not_a_v1_manifest() -> None:
    documents = {"z.md": b"Zulu", "a.md": b"Alpha"}
    first = generate_manifest_draft(documents)
    second = generate_manifest_draft(dict(reversed(tuple(documents.items()))))
    decoded = json.loads(first)

    assert first == second
    assert list(decoded["documents"]) == ["a.md", "z.md"]
    assert decoded["documents"]["a.md"]["sha256"] == hashlib.sha256(b"Alpha").hexdigest()
    with pytest.raises(CorpusProvenanceError):
        parse_provenance_manifest(first, documents)


def test_approval_conversion_returns_policy_valid_v1_manifest() -> None:
    documents = {"guide.md": b"Reviewed guide"}
    reviewed = approve_manifest_draft(
        draft_bytes=generate_manifest_draft(documents),
        documents=documents,
        approvals={"guide.md": _approval()},
        policy=_policy(),
    )
    parsed = parse_provenance_manifest(reviewed.manifest_bytes, documents)

    assert parsed["guide.md"].owner == "Documentation team"
    assert reviewed.entries[0].source_sha256 == hashlib.sha256(documents["guide.md"]).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    ("missing", "unapproved", "wrong_hash", "extra", "bad_url", "stale"),
)
def test_approval_conversion_fails_closed(mutation: str) -> None:
    documents = {"guide.md": b"Reviewed guide"}
    draft = generate_manifest_draft(documents)
    approvals: dict[str, ManifestDocumentApproval] = {"guide.md": _approval()}
    if mutation == "missing":
        approvals = {}
    elif mutation == "unapproved":
        approvals["guide.md"] = _approval(approved=False)
    elif mutation == "wrong_hash":
        decoded = json.loads(draft)
        decoded["documents"]["guide.md"]["sha256"] = "0" * 64
        draft = json.dumps(decoded).encode()
    elif mutation == "extra":
        approvals["other.md"] = _approval()
    elif mutation == "bad_url":
        approvals["guide.md"] = _approval(url="https://private.example.com/guide")
    else:
        approvals["guide.md"] = _approval(reviewed_at=date(2026, 1, 1))

    with pytest.raises(ManifestWorkflowError):
        approve_manifest_draft(
            draft_bytes=draft,
            documents=documents,
            approvals=approvals,
            policy=_policy(),
        )


def test_draft_rejects_duplicate_json_keys() -> None:
    documents = {"guide.md": b"Reviewed guide"}
    digest = hashlib.sha256(documents["guide.md"]).hexdigest()
    draft = (
        '{"draft_version":1,"draft_version":1,"documents":{"guide.md":{"sha256":"'
        + digest
        + '"}}}'
    ).encode()
    with pytest.raises(ManifestWorkflowError) as caught:
        approve_manifest_draft(
            draft_bytes=draft,
            documents=documents,
            approvals={"guide.md": _approval()},
            policy=_policy(),
        )
    assert caught.value.code == "invalid_draft"


def test_reviewed_planner_rejects_policy_before_embedding_and_preserves_planner() -> None:
    calls = 0

    def embed(input: Sequence[str]) -> list[list[float]]:
        nonlocal calls
        calls += 1
        return [[float(index), 1.0] for index, _ in enumerate(input)]

    with pytest.raises(ManifestWorkflowError) as caught:
        plan_reviewed_candidate(
            corpus=ExactCorpusReference(corpus_id="help", corpus_version="v1"),
            source=_source(entry_changes={"url": "https://private.example.com/guide"}),
            policy=_policy(),
            embedding=EmbeddingSpecification(identity="fixture-v1", dimensions=2),
            embed=embed,
        )
    assert caught.value.code == "disallowed_origin"
    assert calls == 0

    plan = plan_reviewed_candidate(
        corpus=ExactCorpusReference(corpus_id="help", corpus_version="v1"),
        source=_source(),
        policy=_policy(),
        embedding=EmbeddingSpecification(identity="fixture-v1", dimensions=2),
        embed=embed,
    )
    assert plan.document_count == 1
    assert calls == 1
