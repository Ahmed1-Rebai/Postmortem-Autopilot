"""The Writer: turn ranked hypotheses and cited evidence into a draft.

The Writer never sees the graph. It sees the events it may cite, the
hypotheses with their computed confidence bands, and — on a retry — the
validator's specific complaints. Everything it produces is checked afterwards.

The Timeline is deliberately withheld from it and inserted by code, so the
document's one section full of timestamps contains no model-generated ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent.llm import DEFAULT_MAX_TOKENS, LLMProvider
from agent.prompts import load as load_prompt
from agent.render.timeline import render_evidence_for_model
from agent.state import Complaint, Event, Hypothesis, SimilarIncident, ValidationReport


@dataclass(frozen=True, slots=True)
class WriterResult:
    draft_md: str
    input_tokens: int
    output_tokens: int


def write_draft(
    *,
    incident_title: str,
    events: Sequence[Event],
    hypotheses: Sequence[Hypothesis],
    similar_incidents: Sequence[SimilarIncident] = (),
    previous_draft: str | None = None,
    validation: ValidationReport | None = None,
    provider: LLMProvider,
    model: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> WriterResult:
    """Write, or rewrite in response to validator complaints."""
    repairing = previous_draft is not None and validation is not None
    system = load_prompt("repair" if repairing else "writer")

    sections = [
        f"# Incident: {incident_title}",
        "",
        "## Evidence you may cite",
        "",
        "Only these IDs exist. Any other ID fails the document.",
        "",
        render_evidence_for_model(events),
        "",
        "## Ranked hypotheses",
        "",
        _render_hypotheses(hypotheses, events),
    ]

    if similar_incidents:
        sections += [
            "",
            "## Similar past incidents",
            "",
            _render_similar(similar_incidents),
        ]

    if repairing:
        assert validation is not None and previous_draft is not None
        sections += [
            "",
            "## Validator complaints to fix",
            "",
            _render_complaints(validation),
            "",
            "## Your previous draft",
            "",
            previous_draft,
        ]

    user = "\n".join(sections)
    response = provider.complete(
        system=system, user=user, model=model, max_tokens=max_tokens
    )
    return WriterResult(
        draft_md=response.text.strip(),
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


def _render_hypotheses(
    hypotheses: Sequence[Hypothesis], events: Sequence[Event]
) -> str:
    if not hypotheses:
        return (
            "(none — the evidence did not support any explanation. Say so "
            "plainly rather than proposing one.)"
        )
    summaries = {event.id: event.summary for event in events}
    blocks = []
    for hypothesis in hypotheses:
        support = "\n".join(
            f"    - {eid}: {summaries.get(eid, '')}"
            for eid in hypothesis.supporting_event_ids
        )
        against = (
            "\n".join(
                f"    - {eid}: {summaries.get(eid, '')}"
                for eid in hypothesis.contradicting_event_ids
            )
            or "    - (none found)"
        )
        blocks.append(
            f"### Rank {hypothesis.rank} — confidence {hypothesis.confidence:.2f} "
            f"({hypothesis.band})\n"
            f"{hypothesis.statement}\n\n"
            f"  Supporting events:\n{support}\n\n"
            f"  Disconfirming evidence:\n{against}"
        )
    return "\n\n".join(blocks)


def _render_similar(similar: Sequence[SimilarIncident]) -> str:
    lines = []
    for incident in similar:
        open_actions = [
            action for action in incident.corrective_actions if action.is_open
        ]
        detail = (
            f" Corrective actions still open: "
            f"{'; '.join(action.description for action in open_actions)}."
            if open_actions
            else ""
        )
        lines.append(
            f"- {incident.incident_id} ({incident.title}), "
            f"overlap {incident.overlap:.2f}.{detail}"
        )
    return "\n".join(lines)


def _render_complaints(validation: ValidationReport) -> str:
    """Specific and mechanical. "Sentence 7 has no citation" is repairable;
    "improve your citations" is not."""
    lines = [
        f"Citation coverage was {validation.coverage:.2%}, "
        f"threshold is {validation.coverage_threshold:.2%}.",
        "",
    ]
    lines.extend(_complaint_line(complaint) for complaint in validation.complaints)
    return "\n".join(lines)


def _complaint_line(complaint: Complaint) -> str:
    where = (
        f"sentence {complaint.sentence_index}"
        if complaint.sentence_index is not None
        else "document"
    )
    detail = f"- [{complaint.kind.value}] {where}: {complaint.detail}"
    if complaint.sentence:
        detail += f'\n  text: "{complaint.sentence.strip()}"'
    return detail
