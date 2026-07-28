"""The Timeline section, rendered from the graph by code.

There is no reason to pay a model to sort a list, and doing so is the single
largest hallucination surface in the document: a fabricated timestamp reads
exactly like a real one. Rendering it here means every row is a graph row, and
every citation in it resolves by construction.

The same module also serializes the candidate subgraph for the Analyst. That
serialization is capped, because an unbounded one makes token cost scale with
how much the service logged during the incident (trap 6 in TODO.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from agent.state import Chain, Event

#: How much of a summary reaches the model. Log lines can be arbitrarily long;
#: the signature already carries the failure's identity.
SUMMARY_LIMIT: Final[int] = 160


def format_timestamp(event: Event) -> str:
    return event.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def render_timeline(events: Sequence[Event]) -> str:
    """The Timeline section as markdown.

    Every row carries its event's ID, so a reader can take any line of the
    postmortem and find the row it came from.
    """
    if not events:
        return "## Timeline\n\nNo events were collected for this incident.\n"

    ordered = sorted(events, key=lambda event: (event.timestamp, event.id))
    lines = [
        "## Timeline",
        "",
        "| Time (UTC) | Type | Service | Event | Source ID |",
        "|---|---|---|---|---|",
    ]
    for event in ordered:
        summary = _truncate(event.summary, SUMMARY_LIMIT).replace("|", "\\|")
        if event.count > 1:
            summary = f"{summary} _(x{event.count})_"
        lines.append(
            f"| {format_timestamp(event)} "
            f"| {event.type.value} "
            f"| {event.service or '—'} "
            f"| {summary} "
            f"| `{event.id}` |"
        )
    lines.append("")
    return "\n".join(lines)


def render_evidence_for_model(events: Sequence[Event]) -> str:
    """The event list handed to the Analyst and the Writer.

    One line per event, ID first, so the model has no excuse for citing
    something it was not given — and so the mock provider can synthesize valid
    citations by reading IDs straight back out of the prompt.
    """
    if not events:
        return "(no events)"
    ordered = sorted(events, key=lambda event: (event.timestamp, event.id))
    lines = []
    for event in ordered:
        occurrences = f" (x{event.count})" if event.count > 1 else ""
        signature = f" sig={event.signature}" if event.signature else ""
        lines.append(
            f"- {event.id} | {format_timestamp(event)} | {event.type.value} "
            f"| service={event.service or 'unknown'} "
            f"| {_truncate(event.summary, SUMMARY_LIMIT)}{occurrences}{signature}"
        )
    return "\n".join(lines)


def render_chains_for_model(chains: Sequence[Chain]) -> str:
    """The candidate causal links, with which heuristics produced each one.

    The heuristics are shown deliberately: the model is being asked to rank
    explanations, and *why* code proposed a link is the most useful thing it
    can know about that link.
    """
    if not chains:
        return "(no candidate causal links were generated)"
    lines = []
    for index, chain in enumerate(chains, start=1):
        lines.append(
            f"{index}. cause={chain.cause_id} -> effect={chain.effect_id}\n"
            f"   heuristics: {', '.join(chain.heuristics) or 'none'}\n"
            f"   delta: {chain.delta_seconds}s | "
            f"this cause is linked to {chain.breadth} other effect(s)\n"
            f"   cause: {_truncate(chain.cause_summary, SUMMARY_LIMIT)}\n"
            f"   effect: {_truncate(chain.effect_summary, SUMMARY_LIMIT)}"
        )
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
