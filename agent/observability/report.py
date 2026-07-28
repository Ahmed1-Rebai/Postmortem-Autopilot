"""The `RunReport` — what one run did, on disk as JSON.

Written on every run, passing or failing. A failed run's report is the more
useful of the two: it is what tells you *why* the draft was rejected, and it is
what the eval harness reads to compute citation coverage and hallucination rate
across the corpus.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent.state import PipelineState, RunReport, ValidationReport


def build_run_report(
    state: PipelineState,
    *,
    started_at: Any,
    finished_at: Any,
) -> RunReport:
    validation = state.get("validation")
    return RunReport(
        incident_id=state.get("incident_id", "unknown"),
        started_at=started_at,
        finished_at=finished_at,
        sources_used=tuple(state.get("sources_used") or []),
        sources_failed=tuple(state.get("sources_failed") or []),
        event_count=len(state.get("events") or []),
        candidate_count=len(state.get("candidates") or []),
        hypothesis_count=len(state.get("hypotheses") or []),
        retry_count=state.get("retry_count", 0),
        token_usage=dict(state.get("token_usage") or {}),
        validation=validation,
    )


def report_to_dict(report: RunReport) -> dict[str, Any]:
    """Flatten for JSON. Explicit rather than a blanket `asdict`, so adding a
    field to the dataclass cannot silently change the on-disk schema the eval
    harness parses."""
    return {
        "incident_id": report.incident_id,
        "succeeded": report.succeeded,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "duration_seconds": round(report.duration_seconds, 3),
        "sources_used": list(report.sources_used),
        "sources_failed": [asdict(failure) for failure in report.sources_failed],
        "event_count": report.event_count,
        "candidate_count": report.candidate_count,
        "hypothesis_count": report.hypothesis_count,
        "retry_count": report.retry_count,
        "token_usage": dict(report.token_usage),
        "total_tokens": sum(report.token_usage.values()),
        "validation": _validation_to_dict(report.validation),
    }


def _validation_to_dict(validation: ValidationReport | None) -> dict[str, Any] | None:
    if validation is None:
        return None
    return {
        "passed": validation.passed,
        "factual_sentences": validation.factual_sentences,
        "cited_sentences": validation.cited_sentences,
        "coverage": round(validation.coverage, 4),
        "coverage_threshold": validation.coverage_threshold,
        "citations_total": validation.citations_total,
        "citations_hallucinated": validation.citations_hallucinated,
        "hallucination_rate": round(validation.hallucination_rate, 4),
        "timestamp_mismatches": validation.timestamp_mismatches,
        "complaints": [
            {
                "kind": complaint.kind.value,
                "detail": complaint.detail,
                "sentence_index": complaint.sentence_index,
                "sentence": complaint.sentence,
                "cited_id": complaint.cited_id,
            }
            for complaint in validation.complaints
        ],
    }


def write_run_report(report: RunReport, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"run_report_{report.incident_id}.json"
    path.write_text(
        json.dumps(report_to_dict(report), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def write_document(incident_id: str, document: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"postmortem_{incident_id}.md"
    path.write_text(document, encoding="utf-8")
    return path
