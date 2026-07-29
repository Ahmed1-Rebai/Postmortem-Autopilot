"""Assemble the final document: code-rendered sections around the LLM's prose.

The header and the Timeline are generated here. The header is not decoration —
it tells a reader which sections to distrust, and it states which evidence
sources were available, so a 0.8 computed from three sources is not mistaken
for a 0.8 computed from five.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from agent.render.recurrence import render_similar_incidents
from agent.render.timeline import render_timeline
from agent.state import Event, Hypothesis, Incident, RunReport, SimilarIncident

#: Where the code-rendered Timeline and Similar Past Incidents sections are
#: spliced in. After Impact, before Hypotheses — the reader wants the sequence
#: of events and any recurrence context before the argument about what caused
#: them.
_TIMELINE_ANCHOR: Final[str] = "## Hypotheses"

_HEADING: Final[re.Pattern[str]] = re.compile(r"^##\s+", re.MULTILINE)


def render_document(
    *,
    incident: Incident,
    draft_md: str,
    events: Sequence[Event],
    hypotheses: Sequence[Hypothesis],
    sources_used: Sequence[str],
    sources_failed: Sequence[str] = (),
    similar_incidents: Sequence[SimilarIncident] = (),
    generated_at: datetime | None = None,
) -> str:
    """Splice the code-rendered sections into the model's draft."""
    parts = [
        _render_header(
            incident=incident,
            hypotheses=hypotheses,
            sources_used=sources_used,
            sources_failed=sources_failed,
            generated_at=generated_at,
        ),
        _with_generated_sections(draft_md, events, similar_incidents),
    ]
    return "\n\n".join(part.strip() for part in parts if part.strip()) + "\n"


def _with_generated_sections(
    draft_md: str, events: Sequence[Event], similar_incidents: Sequence[SimilarIncident]
) -> str:
    """Insert the Timeline and Similar Past Incidents sections, replacing any
    Timeline the model wrote anyway.

    The prompt forbids writing a timeline; this makes the prompt unnecessary. A
    model-written timeline is a table of unchecked timestamps, which is exactly
    the surface this project removes. Similar Past Incidents is never in the
    model's draft to begin with — the Writer is never given the data, because
    whether a past fix is still open is a structural fact invariant 1 reserves
    for code.
    """
    stripped = _strip_model_timeline(draft_md)
    generated = "\n".join(
        part
        for part in (
            render_timeline(events),
            render_similar_incidents(similar_incidents),
        )
        if part
    )
    index = stripped.find(_TIMELINE_ANCHOR)
    if index == -1:
        return f"{stripped.rstrip()}\n\n{generated}"
    return f"{stripped[:index].rstrip()}\n\n{generated}\n{stripped[index:]}"


def _strip_model_timeline(draft_md: str) -> str:
    lowered = draft_md.lower()
    start = lowered.find("## timeline")
    if start == -1:
        return draft_md
    next_heading = _HEADING.search(draft_md, start + len("## timeline"))
    end = next_heading.start() if next_heading else len(draft_md)
    return draft_md[:start] + draft_md[end:]


def _render_header(
    *,
    incident: Incident,
    hypotheses: Sequence[Hypothesis],
    sources_used: Sequence[str],
    sources_failed: Sequence[str],
    generated_at: datetime | None,
) -> str:
    lines = [
        f"# Postmortem: {incident.title}",
        "",
        f"**Incident:** `{incident.id}` · "
        f"**Window:** {incident.start_time:%Y-%m-%dT%H:%M:%SZ} to "
        f"{incident.end_time:%Y-%m-%dT%H:%M:%SZ} · "
        f"**Severity:** {incident.severity}",
    ]
    if generated_at is not None:
        lines.append(f"**Generated:** {generated_at:%Y-%m-%dT%H:%M:%SZ}")

    lines += [
        "",
        f"**Evidence sources used:** {', '.join(sources_used) or 'none'}",
    ]
    if sources_failed:
        lines.append(
            f"**Sources unavailable:** {', '.join(sources_failed)} — "
            "confidence is computed over the sources that worked, so these "
            "were excluded rather than counted against the score."
        )

    top = hypotheses[0] if hypotheses else None
    if top is None:
        lines += [
            "",
            "> **No supported explanation.** The evidence did not support any "
            "causal hypothesis. This document records what was observed, not "
            "why it happened.",
        ]
    elif top.band == "tentative":
        # docs/03: a tentative top hypothesis must open with a banner. A system
        # that admits inconclusiveness is more useful than one that always
        # picks a winner.
        lines += [
            "",
            f"> **Analysis inconclusive.** The leading hypothesis scores "
            f"{top.confidence:.2f} (tentative) and needs human verification. "
            "Treat the Hypotheses section as a starting point, not a "
            "conclusion.",
        ]
    else:
        lines += [
            "",
            f"**Leading hypothesis:** {top.confidence:.2f} ({top.band}) — "
            f"{top.statement}",
        ]
    return "\n".join(lines)


def render_run_report_summary(report: RunReport) -> str:
    """A short operator-facing summary of one run."""
    validation = report.validation
    verdict = "PASS" if report.succeeded else "FAIL"
    coverage = f"{validation.coverage:.1%}" if validation else "n/a"
    hallucinated = validation.citations_hallucinated if validation else "n/a"
    return (
        f"{report.incident_id}: {verdict} · "
        f"{report.event_count} events · {report.candidate_count} candidates · "
        f"{report.hypothesis_count} hypotheses · coverage {coverage} · "
        f"hallucinated {hallucinated} · retries {report.retry_count} · "
        f"{report.duration_seconds:.1f}s · "
        f"{sum(report.token_usage.values())} tokens"
    )
