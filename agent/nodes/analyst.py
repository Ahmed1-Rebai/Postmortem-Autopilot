"""The Analyst: rank competing explanations.

The model orders and phrases hypotheses. It does not source facts and it does
not choose confidence — `confidence.py` computes that from which signals fired
(invariant 7).

The important code in this file is not the call, it is what happens to the
reply. Every event ID the model returns is checked against the set it was
given, **before** anything reaches the graph or the Writer. The prompt asks for
good behaviour; this enforces it. A model that invents an ID here would
otherwise hand the Writer a citation that cannot resolve, and the failure would
surface three stages later as a mysterious validation error.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent.confidence import (
    SignalSet,
    available_signals,
    compute_confidence,
    signals_for,
)
from agent.config import ConfidenceConfig
from agent.llm import DEFAULT_MAX_TOKENS, LLMError, LLMProvider, extract_json
from agent.prompts import load as load_prompt
from agent.render.timeline import render_chains_for_model, render_evidence_for_model
from agent.state import Chain, Event, Hypothesis, make_hypothesis_id


@dataclass(frozen=True, slots=True)
class AnalystResult:
    hypotheses: tuple[Hypothesis, ...]
    dropped_ids: tuple[str, ...]
    input_tokens: int
    output_tokens: int


def analyze(
    *,
    incident_id: str,
    events: Sequence[Event],
    chains: Sequence[Chain],
    provider: LLMProvider,
    model: str,
    confidence_config: ConfidenceConfig,
    sources_used: Sequence[str],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AnalystResult:
    """Rank hypotheses over the candidate subgraph."""
    if not events:
        return AnalystResult((), (), 0, 0)

    system = load_prompt("analyst")
    user = _build_prompt(events, chains)
    response = provider.complete(
        system=system, user=user, model=model, max_tokens=max_tokens
    )

    payload = extract_json(response.text)
    raw = _hypotheses_from(payload)
    known = {event.id for event in events}
    by_id = {event.id: event for event in events}
    available = available_signals(sources_used)

    hypotheses: list[Hypothesis] = []
    dropped: set[str] = set()

    for rank, item in enumerate(raw, start=1):
        statement = str(item.get("statement") or "").strip()
        if not statement:
            continue

        supporting, unknown_support = _partition(
            item.get("supporting_event_ids"), known
        )
        contradicting, unknown_against = _partition(
            item.get("contradicting_event_ids"), known
        )
        dropped |= unknown_support | unknown_against

        # An event cannot both support and contradict the same claim; if the
        # model says both, trust the objection — it is the rarer, more
        # deliberate assertion, and over-trusting support is what inflates
        # scores.
        supporting = [eid for eid in supporting if eid not in set(contradicting)]
        if not supporting:
            continue

        cause = _earliest(supporting, by_id)
        fired = signals_for(
            cause,
            [by_id[eid] for eid in supporting if eid != cause.id],
            events,
            _heuristics_for(cause.id, chains),
        )
        breakdown = compute_confidence(
            SignalSet(fired, available), len(contradicting), confidence_config
        )

        hypotheses.append(
            Hypothesis(
                id=make_hypothesis_id(incident_id, statement),
                rank=rank,
                statement=statement,
                supporting_event_ids=tuple(supporting),
                contradicting_event_ids=tuple(contradicting),
                confidence=breakdown.confidence,
                band=breakdown.band,
            )
        )

    ranked = _rerank(hypotheses)
    return AnalystResult(
        hypotheses=tuple(ranked),
        dropped_ids=tuple(sorted(dropped)),
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _build_prompt(events: Sequence[Event], chains: Sequence[Chain]) -> str:
    return (
        "## Events\n\n"
        f"{render_evidence_for_model(events)}\n\n"
        "## Candidate causal links (generated deterministically by code)\n\n"
        f"{render_chains_for_model(chains)}\n\n"
        "Rank the competing explanations. Use only the event IDs above."
    )


def _hypotheses_from(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("hypotheses", [])
    elif isinstance(payload, list):
        items = payload
    else:
        raise LLMError(f"unexpected analyst payload type: {type(payload).__name__}")
    if not isinstance(items, list):
        raise LLMError("analyst payload 'hypotheses' is not a list")
    return [item for item in items if isinstance(item, dict)]


def _partition(value: Any, known: set[str]) -> tuple[list[str], set[str]]:
    """Split returned IDs into ones the model was actually given, and the rest.

    The rest are dropped and reported, never passed on. This is invariant 1
    enforced by code rather than requested in a prompt.
    """
    if not isinstance(value, list):
        return [], set()
    ids = [str(item).strip() for item in value if str(item).strip()]
    valid = [eid for eid in dict.fromkeys(ids) if eid in known]
    unknown = {eid for eid in ids if eid not in known}
    return valid, unknown


def _earliest(ids: Sequence[str], by_id: dict[str, Event]) -> Event:
    """The earliest supporting event is treated as the proposed cause.

    Causes precede effects, so this needs no help from the model — and asking
    it to nominate one would be another chance to disagree with the graph.
    """
    return min((by_id[eid] for eid in ids), key=lambda event: event.timestamp)


def _heuristics_for(cause_id: str, chains: Sequence[Chain]) -> set[str]:
    fired: set[str] = set()
    for chain in chains:
        if chain.cause_id == cause_id:
            fired |= set(chain.heuristics)
    return fired


def _rerank(hypotheses: Sequence[Hypothesis]) -> list[Hypothesis]:
    """Reorder by computed confidence, preserving the model's order on ties.

    The model proposes an order; the code's number decides it. If the model
    ranked a 0.2 above a 0.9, the number wins — which is invariant 7 applied to
    ordering, not just to the value.
    """
    ordered = sorted(
        enumerate(hypotheses), key=lambda pair: (-pair[1].confidence, pair[0])
    )
    return [
        Hypothesis(
            id=hypothesis.id,
            rank=position,
            statement=hypothesis.statement,
            supporting_event_ids=hypothesis.supporting_event_ids,
            contradicting_event_ids=hypothesis.contradicting_event_ids,
            confidence=hypothesis.confidence,
            band=hypothesis.band,
        )
        for position, (_, hypothesis) in enumerate(ordered, start=1)
    ]
