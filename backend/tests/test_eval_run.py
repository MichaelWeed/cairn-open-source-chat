"""Tests for eval/run_eval.py (task 2.6). Covers the pure logic — YAML
loading, metric computation, judge-verdict parsing, report rendering —
without touching a live Ollama; `run()`/`main()` themselves need a real
model and aren't exercised here. See eval/run_eval.py's own docstring for
why the harness lives outside backend/ and is imported via sys.path.
"""

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
sys.path.insert(0, str(EVAL_DIR))

from run_eval import (  # noqa: E402
    EvalQuestion,
    EvalReport,
    EvalResult,
    judge_groundedness,
    load_questions,
    render_markdown,
)

from app.config import Settings  # noqa: E402


def _question(id: str, answerable: bool, expected_document_id: str | None = None) -> EvalQuestion:
    return EvalQuestion(
        id=id,
        question=f"question {id}",
        answerable=answerable,
        expected_document_id=expected_document_id,
    )


def _result(
    question: EvalQuestion,
    *,
    refused: bool = False,
    citation_ids: list[str] | None = None,
    ttft: float | None = 0.1,
    total: float = 0.5,
) -> EvalResult:
    return EvalResult(
        question=question,
        refused=refused,
        answer_text="an answer",
        citation_ids=citation_ids or [],
        context="<retrieved-context></retrieved-context>",
        ttft_seconds=ttft,
        total_seconds=total,
    )


def test_load_questions_reads_the_bundled_sample_set() -> None:
    questions = load_questions(EVAL_DIR / "questions" / "sample.yaml")
    assert len(questions) >= 4
    answerable = [q for q in questions if q.answerable]
    unanswerable = [q for q in questions if not q.answerable]
    assert answerable and unanswerable
    assert all(q.expected_document_id for q in answerable)
    assert all(q.expected_document_id is None for q in unanswerable)


def test_load_questions_bundled_corpus_covers_expected_documents() -> None:
    corpus_dir = EVAL_DIR / "corpus"
    corpus_stems = {p.stem for p in corpus_dir.glob("*.md")}
    questions = load_questions(EVAL_DIR / "questions" / "sample.yaml")
    expected_ids = {q.expected_document_id for q in questions if q.expected_document_id}
    assert expected_ids <= corpus_stems


def test_judge_groundedness_parses_grounded() -> None:
    assert judge_groundedness(lambda s, u: "GROUNDED", "q", "c", "a") is True


def test_judge_groundedness_parses_ungrounded() -> None:
    assert judge_groundedness(lambda s, u: "UNGROUNDED", "q", "c", "a") is False


def test_judge_groundedness_checks_ungrounded_before_substring_match() -> None:
    # "UNGROUNDED" contains "GROUNDED" as a substring — must not misparse.
    assert judge_groundedness(lambda s, u: "Verdict: UNGROUNDED.", "q", "c", "a") is False


def test_judge_groundedness_unparseable_defaults_to_ungrounded() -> None:
    assert judge_groundedness(lambda s, u: "I'm not sure", "q", "c", "a") is False


def test_correct_refusal_rate_counts_unanswerable_questions_only() -> None:
    answerable = _question("a1", answerable=True, expected_document_id="doc")
    unanswerable_refused = _question("u1", answerable=False)
    unanswerable_answered = _question("u2", answerable=False)

    report = EvalReport(
        results=[
            _result(answerable, citation_ids=["doc"]),
            _result(unanswerable_refused, refused=True),
            _result(unanswerable_answered, refused=False),
        ],
        groundedness={},
    )
    assert report.correct_refusal_rate == 0.5
    assert report.over_refusal_rate == 0.0


def test_over_refusal_rate_flags_answerable_questions_that_got_refused() -> None:
    answerable_refused = _question("a1", answerable=True, expected_document_id="doc")
    report = EvalReport(results=[_result(answerable_refused, refused=True)], groundedness={})
    assert report.over_refusal_rate == 1.0
    assert report.correct_refusal_rate is None  # no unanswerable questions in this set


def test_citation_precision_and_recall() -> None:
    hit = _question("hit", answerable=True, expected_document_id="doc-1")
    miss = _question("miss", answerable=True, expected_document_id="doc-1")
    report = EvalReport(
        results=[
            _result(hit, citation_ids=["doc-1", "doc-2"]),  # correct doc present, precision 1/2
            _result(miss, citation_ids=["doc-2"]),  # wrong doc entirely
        ],
        groundedness={},
    )
    assert report.citation_recall == 0.5
    assert report.citation_precision == 0.25  # mean(1/2, 0/1)


def test_citation_metrics_none_when_no_expected_citations() -> None:
    report = EvalReport(results=[_result(_question("u", answerable=False))], groundedness={})
    assert report.citation_precision is None
    assert report.citation_recall is None


def test_groundedness_rate_only_covers_answered_questions() -> None:
    q1, q2 = _question("q1", answerable=True), _question("q2", answerable=True)
    report = EvalReport(results=[_result(q1), _result(q2)], groundedness={"q1": True, "q2": False})
    assert report.groundedness_rate == 0.5


def test_latency_stats_none_when_all_refused() -> None:
    report = EvalReport(
        results=[_result(_question("u", answerable=False), refused=True, ttft=None)],
        groundedness={},
    )
    assert report.latency_stats("ttft_seconds") is None


def test_latency_stats_computes_mean_and_p95() -> None:
    q = _question("q", answerable=True, expected_document_id="doc")
    results = [_result(q, citation_ids=["doc"], total=total) for total in [0.1, 0.2, 0.3, 0.4, 1.0]]
    report = EvalReport(results=results, groundedness={})
    stats = report.latency_stats("total_seconds")
    assert stats is not None
    mean, p95 = stats
    assert mean == 0.4
    assert p95 == 1.0  # too few samples for a real P95 — it collapses to max()


def test_render_markdown_smoke() -> None:
    q_answerable = _question("a1", answerable=True, expected_document_id="doc-1")
    q_unanswerable = _question("u1", answerable=False)
    report = EvalReport(
        results=[
            _result(q_answerable, citation_ids=["doc-1"]),
            _result(q_unanswerable, refused=True, ttft=None),
        ],
        groundedness={"a1": True},
    )
    markdown = render_markdown(
        report,
        corpus_dir=EVAL_DIR / "corpus",
        questions_path=EVAL_DIR / "questions" / "sample.yaml",
        settings=Settings(),
    )
    assert "# Eval report" in markdown
    assert "Groundedness rate" in markdown
    assert "a1" in markdown and "u1" in markdown
    assert "n/a — task 4.4 not built yet" in markdown
