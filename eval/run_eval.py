#!/usr/bin/env python3
"""Eval harness v0. See DEVELOPER_README.md §6 for the target shape:
groundedness (LLM-judged), citation
precision/recall, correct-refusal rate, TTFT/P95 latency. Adversarial
pass/fail isn't reported — that suite is task 4.4, not built yet.

Self-contained by design: there's no HTTP upload endpoint yet (task 2.7),
so this ingests eval/corpus/ directly via ingest_upload() into a fresh,
ephemeral app instance (its own SQLite/Chroma paths) rather than assuming
some already-running, already-ingested deployment. Once 2.7 exists,
pointing this at a live deployment's own corpus instead is a natural
follow-up, not a redesign.

Run: `make eval` from the repo root, or directly:
    cd backend && uv run python ../eval/run_eval.py

Requires a reachable Ollama (OLLAMA_BASE_URL) with OLLAMA_MODEL and
EMBEDDING_MODEL pulled. Real embeddings, not the deterministic
FakeEmbeddingFunction, are required for this to measure anything
meaningful — EMBEDDING_PROVIDER defaults to "ollama" here specifically
(unlike the app's own network-free-by-default boot).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
import yaml

os.environ.setdefault("EMBEDDING_PROVIDER", "ollama")

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import bootstrap  # noqa: E402
from app.ingest.pipeline import ingest_upload  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.ollama import OllamaProvider  # noqa: E402
from app.retrieval import build_context_block, retrieve_chunks  # noqa: E402

DEFAULT_CORPUS_DIR = EVAL_DIR / "corpus"
DEFAULT_QUESTIONS_PATH = EVAL_DIR / "questions" / "sample.yaml"
DEFAULT_REPORTS_DIR = EVAL_DIR / "reports"

JudgeCall = Callable[[str, str], str]  # (system_prompt, user_prompt) -> raw model text

JUDGE_SYSTEM_PROMPT = (
    "You are a strict grading assistant. You will be given a QUESTION, the "
    "CONTEXT an assistant was allowed to use, and the assistant's ANSWER. "
    "Respond with exactly one word: GROUNDED if every factual claim in "
    "ANSWER is supported by CONTEXT, or UNGROUNDED if ANSWER makes any "
    "claim not supported by, or contradicting, CONTEXT."
)


@dataclass(frozen=True)
class EvalQuestion:
    id: str
    question: str
    answerable: bool
    expected_document_id: str | None = None


@dataclass(frozen=True)
class EvalResult:
    question: EvalQuestion
    refused: bool
    answer_text: str
    citation_ids: list[str]
    context: str
    ttft_seconds: float | None
    total_seconds: float


def load_questions(path: Path) -> list[EvalQuestion]:
    raw = yaml.safe_load(path.read_text())
    return [
        EvalQuestion(
            id=item["id"],
            question=item["question"],
            answerable=item["answerable"],
            expected_document_id=item.get("expected_document_id"),
        )
        for item in raw
    ]


def _parse_sse_block(block: str) -> tuple[str, str]:
    lines = block.splitlines()
    event_type = next(
        (line.removeprefix("event: ") for line in lines if line.startswith("event: ")), "message"
    )
    data = next((line.removeprefix("data: ") for line in lines if line.startswith("data: ")), "")
    return event_type, data


@dataclass(frozen=True)
class EvalReport:
    results: list[EvalResult]
    groundedness: dict[str, bool]  # question id -> judged grounded (answered questions only)

    @property
    def answered(self) -> list[EvalResult]:
        return [r for r in self.results if not r.refused]

    @property
    def groundedness_rate(self) -> float | None:
        judged = list(self.groundedness.values())
        return (sum(judged) / len(judged)) if judged else None

    @property
    def correct_refusal_rate(self) -> float | None:
        unanswerable = [r for r in self.results if not r.question.answerable]
        if not unanswerable:
            return None
        return sum(1 for r in unanswerable if r.refused) / len(unanswerable)

    @property
    def over_refusal_rate(self) -> float | None:
        answerable = [r for r in self.results if r.question.answerable]
        if not answerable:
            return None
        return sum(1 for r in answerable if r.refused) / len(answerable)

    @property
    def citation_precision(self) -> float | None:
        scored = [
            (1 if r.question.expected_document_id in r.citation_ids else 0, len(r.citation_ids))
            for r in self.results
            if r.question.expected_document_id and r.citation_ids
        ]
        if not scored:
            return None
        return statistics.mean(correct / returned for correct, returned in scored)

    @property
    def citation_recall(self) -> float | None:
        scored = [
            r.question.expected_document_id in r.citation_ids
            for r in self.results
            if r.question.expected_document_id
        ]
        if not scored:
            return None
        return sum(scored) / len(scored)

    def latency_stats(self, attr: str) -> tuple[float, float] | None:
        values = sorted(v for r in self.answered if (v := getattr(r, attr)) is not None)
        if not values:
            return None
        p95_index = min(len(values) - 1, max(0, round(0.95 * (len(values) - 1))))
        return statistics.mean(values), values[p95_index]


def judge_groundedness(judge_call: JudgeCall, question: str, context: str, answer: str) -> bool:
    prompt = f"QUESTION:\n{question}\n\nCONTEXT:\n{context}\n\nANSWER:\n{answer}"
    verdict = judge_call(JUDGE_SYSTEM_PROMPT, prompt).strip().upper()
    if "UNGROUNDED" in verdict:
        return False
    if "GROUNDED" in verdict:
        return True
    print(
        f"warning: unparseable judge verdict {verdict!r}, treating as ungrounded", file=sys.stderr
    )
    return False


def ollama_judge_call(base_url: str, model: str) -> JudgeCall:
    client = httpx.Client(timeout=120.0)

    def call(system_prompt: str, user_prompt: str) -> str:
        response = client.post(
            f"{base_url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        response.raise_for_status()
        return str(response.json()["message"]["content"])

    return call


def ingest_corpus(app: FastAPI, settings: Settings, corpus_dir: Path) -> None:
    db = bootstrap(settings.database_path)
    for path in sorted(corpus_dir.glob("*.md")):
        ingest_upload(
            db=db,
            collection=app.state.document_collection,
            document_id=path.stem,
            filename=path.name,
            content=path.read_bytes(),
        )
    db.close()


def run_one(
    client: TestClient, app: FastAPI, settings: Settings, question: EvalQuestion
) -> EvalResult:
    started = time.monotonic()
    first_chunk_at: float | None = None
    answer_parts: list[str] = []
    citation_ids: list[str] = []
    refused = False

    with client.stream(
        "POST",
        "/api/v1/chat/message",
        json={"session_id": f"eval-{question.id}", "message": question.question, "history": []},
    ) as response:
        buffer = ""
        for text in response.iter_text():
            buffer += text
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                if not block.strip():
                    continue
                event_type, data = _parse_sse_block(block)
                payload = json.loads(data) if data else {}
                if event_type == "chunk":
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic() - started
                    answer_parts.append(payload.get("delta", ""))
                elif event_type == "citations":
                    citation_ids = [s["id"] for s in payload.get("sources", [])]
                elif event_type == "done":
                    refused = payload.get("finish_reason") == "refused"
    total = time.monotonic() - started

    # Reconstructed from the same in-process collection with the same
    # query/settings the request itself used — not a second retrieval
    # implementation, just calling the real one again for the judge's
    # benefit, since the HTTP contract doesn't expose raw chunk text.
    chunks = retrieve_chunks(
        app.state.document_collection, question.question, top_k=settings.retrieval_top_k
    )
    context = build_context_block(chunks)

    return EvalResult(
        question=question,
        refused=refused,
        answer_text="".join(answer_parts),
        citation_ids=citation_ids,
        context=context,
        ttft_seconds=first_chunk_at,
        total_seconds=total,
    )


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _fmt_latency(stats: tuple[float, float] | None) -> str:
    return "n/a" if stats is None else f"{stats[0]:.2f}s / {stats[1]:.2f}s"


def _fmt_num(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def render_markdown(
    report: EvalReport, *, corpus_dir: Path, questions_path: Path, settings: Settings
) -> str:
    repo_root = EVAL_DIR.parent
    lines = [
        f"# Eval report — {date.today().isoformat()}",
        "",
        f"Corpus: `{corpus_dir.relative_to(repo_root)}` "
        f"({len(list(corpus_dir.glob('*.md')))} documents). "
        f"Questions: `{questions_path.relative_to(repo_root)}` ({len(report.results)} questions).",
        "",
        f"Provider: `{settings.ollama_model}` via Ollama at `{settings.ollama_base_url}`. "
        f"Embeddings: `{settings.embedding_model}`. "
        f"retrieval_top_k={settings.retrieval_top_k}, "
        f"retrieval_max_distance={settings.retrieval_max_distance}.",
        "",
        "**Sample size caveat:** this is task 2.6's v0 proof that the harness "
        "works end to end against a real model, not a statistically "
        "meaningful eval — a handful of questions is too few for P95 to mean "
        "anything. Write a real question set for your own corpus per "
        "DEVELOPER_README.md §6 before trusting this shape of report in "
        "production.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Groundedness rate (answered questions) | {_fmt_pct(report.groundedness_rate)} |",
        "| Correct-refusal rate (unanswerable questions) | "
        f"{_fmt_pct(report.correct_refusal_rate)} |",
        "| Over-refusal rate (answerable questions refused anyway) | "
        f"{_fmt_pct(report.over_refusal_rate)} |",
        f"| Citation precision | {_fmt_pct(report.citation_precision)} |",
        f"| Citation recall | {_fmt_pct(report.citation_recall)} |",
        f"| TTFT mean / P95 | {_fmt_latency(report.latency_stats('ttft_seconds'))} |",
        f"| Total latency mean / P95 | {_fmt_latency(report.latency_stats('total_seconds'))} |",
        "| Adversarial suite | n/a — task 4.4 not built yet |",
        "",
        "## Per-question detail",
        "",
        "| id | answerable | refused | grounded | citations | TTFT (s) | total (s) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in report.results:
        grounded = report.groundedness.get(r.question.id)
        grounded_str = "-" if grounded is None else ("yes" if grounded else "no")
        lines.append(
            f"| {r.question.id} | {r.question.answerable} | {r.refused} | {grounded_str} | "
            f"{', '.join(r.citation_ids) or '-'} | {_fmt_num(r.ttft_seconds)} | "
            f"{_fmt_num(r.total_seconds)} |"
        )
    return "\n".join(lines) + "\n"


def run(
    *,
    settings: Settings,
    corpus_dir: Path,
    questions_path: Path,
    judge_call: JudgeCall,
) -> EvalReport:
    provider = OllamaProvider(base_url=settings.ollama_base_url, model=settings.ollama_model)
    app = create_app(settings, provider=provider)
    questions = load_questions(questions_path)

    with TestClient(app) as client:
        ingest_corpus(app, settings, corpus_dir)

        results = [run_one(client, app, settings, q) for q in questions]

        groundedness = {
            r.question.id: judge_groundedness(
                judge_call, r.question.question, r.context, r.answer_text
            )
            for r in results
            if not r.refused
        }

    return EvalReport(results=results, groundedness=groundedness)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cairn eval harness v0 (task 2.6)")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            database_path=Path(tmp) / "eval.db",
            chroma_path=Path(tmp) / "chroma",
        )
        judge_call = ollama_judge_call(settings.ollama_base_url, settings.ollama_model)
        report = run(
            settings=settings,
            corpus_dir=args.corpus,
            questions_path=args.questions,
            judge_call=judge_call,
        )

    markdown = render_markdown(
        report, corpus_dir=args.corpus, questions_path=args.questions, settings=settings
    )
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.reports_dir / f"{date.today().isoformat()}.md"
    report_path.write_text(markdown)
    print(markdown)
    print(f"Report written to {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
