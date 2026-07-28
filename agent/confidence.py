"""The confidence model — pure functions, docs/03.

    raw_support = Σ(wᵢ·sᵢ for signals that fired and were available)
                  ─────────────────────────────────────────────────
                  Σ(wᵢ    for signals AVAILABLE this run)

    confidence  = clamp(raw_support - contradiction_penalty, 0.0, 1.0)

**The model never chooses this number** (invariant 7). The LLM orders and
explains hypotheses; this file produces the score, from flags the evidence
plane set. Every result carries its own decomposition, because a confidence
score you cannot take apart is a number the reader has to take on faith — which
is the thing this project exists not to ask of them.

What this is: a transparent, reproducible, decomposable weighted score over
named heuristics. Given the same graph you get the same number.

What it is not: a calibrated probability. 0.8 does not mean "right 80% of the
time". Saying so would need a labelled corpus far larger than this project has.
`docs/03` records what calibration would require; the eval harness records the
data it would need.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from agent.config import SIGNAL_NAMES, ConfidenceConfig
from agent.state import Band, Event, EventType

#: Which collector each signal depends on. A signal with no listed source needs
#: nothing but timestamps, which every event has.
#:
#: This is what makes the denominator availability-aware: no chat integration
#: means `human_confirmation` leaves the denominator entirely, rather than
#: capping every score for a reason unrelated to the evidence.
SIGNAL_SOURCES: Final[dict[str, frozenset[str]]] = {
    "change_path_overlap": frozenset({"git"}),
    "log_signature_match": frozenset({"logs"}),
    "metric_correlation": frozenset({"alerts"}),
    "temporal_proximity": frozenset(),
    "human_confirmation": frozenset({"chat"}),
}


@dataclass(frozen=True, slots=True)
class SignalSet:
    """Which signals fired for a hypothesis, and which could have."""

    fired: frozenset[str]
    available: frozenset[str]

    def __post_init__(self) -> None:
        unknown = (self.fired | self.available) - SIGNAL_NAMES
        if unknown:
            raise ValueError(f"unknown signals {sorted(unknown)}")

    @property
    def counted(self) -> frozenset[str]:
        """Signals that fired *and* were available.

        A signal cannot fire without its source, so the intersection is
        normally just `fired` — it is taken defensively so a caller that
        mislabels availability produces a low score rather than one above 1.0.
        """
        return self.fired & self.available


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """A score and the complete argument for it."""

    confidence: float
    band: Band
    raw_support: float
    contradiction_penalty: float
    numerator: float
    denominator: float
    contributions: dict[str, float]
    fired: tuple[str, ...]
    available: tuple[str, ...]
    excluded: tuple[str, ...]
    contradicting_events: int

    def explain(self) -> str:
        """One line a human can check against the postmortem header."""
        parts = ", ".join(
            f"{name}={value:+.2f}" for name, value in sorted(self.contributions.items())
        )
        detail = f"{self.numerator:.2f}/{self.denominator:.2f}"
        if self.contradiction_penalty:
            detail += f" - {self.contradiction_penalty:.2f} contradiction"
        return f"{self.confidence:.2f} ({self.band}) = {detail} [{parts}]"


def available_signals(sources_used: Iterable[str]) -> frozenset[str]:
    """Signals collectible from the sources that actually worked this run.

    Takes the sources that *succeeded*, not the ones that were configured: a
    collector that errored leaves its signal out of the denominator, so a
    degraded run produces lower certainty rather than a penalized score.
    """
    present = set(sources_used)
    return frozenset(
        signal
        for signal, required in SIGNAL_SOURCES.items()
        if not required or (required & present)
    )


def band_for(confidence: float, config: ConfidenceConfig) -> Band:
    if confidence >= config.band_likely:
        return "likely"
    if confidence >= config.band_plausible:
        return "plausible"
    return "tentative"


def contradiction_penalty(count: int, config: ConfidenceConfig) -> float:
    """Capped so contradicting evidence can demote a hypothesis but never zero
    it: strong support plus one contradiction is genuinely still in play, and
    that ambiguity is exactly what should reach the human reader."""
    if count < 0:
        raise ValueError(f"contradicting event count cannot be negative: {count}")
    return min(config.contradiction_cap, config.contradiction_per_event * count)


def compute_confidence(
    signals: SignalSet,
    contradicting_events: int,
    config: ConfidenceConfig,
) -> ConfidenceBreakdown:
    """Score one hypothesis. Pure: same inputs, same number, always."""
    denominator = sum(config.weights[name] for name in signals.available)
    counted = signals.counted
    contributions = {name: config.weights[name] for name in counted}
    numerator = sum(contributions.values())

    # A zero denominator means no signal was collectible, so there is nothing
    # to be confident from. Zero is honest; anything else would be an opinion.
    raw_support = numerator / denominator if denominator > 0.0 else 0.0

    penalty = contradiction_penalty(contradicting_events, config)
    confidence = round(min(1.0, max(0.0, raw_support - penalty)), 4)

    return ConfidenceBreakdown(
        confidence=confidence,
        # Banded from the rounded value so the number shown and the word shown
        # can never disagree.
        band=band_for(confidence, config),
        raw_support=round(raw_support, 4),
        contradiction_penalty=round(penalty, 4),
        numerator=round(numerator, 4),
        denominator=round(denominator, 4),
        contributions=contributions,
        fired=tuple(sorted(signals.fired)),
        available=tuple(sorted(signals.available)),
        excluded=tuple(sorted(SIGNAL_NAMES - signals.available)),
        contradicting_events=contradicting_events,
    )


def signals_for(
    cause: Event,
    supporting: Sequence[Event],
    all_events: Sequence[Event],
    heuristics: Iterable[str],
) -> frozenset[str]:
    """Which confidence signals the evidence supports for one hypothesis.

    Note the linker's heuristics and the confidence model's signals are
    *different sets*. `service_overlap` helps propose a candidate but is not
    scored: it is nearly implied by `change_path_overlap` on a repo laid out by
    service, and counting both would double-weight one piece of evidence.
    """
    fired: set[str] = set()
    heuristic_names = set(heuristics)

    if "temporal_proximity" in heuristic_names:
        fired.add("temporal_proximity")
    if "change_path_overlap" in heuristic_names:
        fired.add("change_path_overlap")

    if _has_new_error_signature(cause, supporting, all_events):
        fired.add("log_signature_match")
    if _has_correlated_alert(cause, supporting):
        fired.add("metric_correlation")
    if any(event.type is EventType.CHAT_MESSAGE for event in supporting):
        fired.add("human_confirmation")

    return frozenset(fired)


def _has_new_error_signature(
    cause: Event, supporting: Sequence[Event], all_events: Sequence[Event]
) -> bool:
    """An error signature that appears after the cause and not before it.

    The "not before" half is what stops a pre-existing, unrelated error from
    corroborating every candidate cause in the window. An error that was
    already happening is not evidence that this change started it.
    """
    prior = {
        event.signature
        for event in all_events
        if event.signature and event.timestamp <= cause.timestamp
    }
    return any(
        event.type is EventType.LOG_ERROR
        and event.signature is not None
        and event.timestamp > cause.timestamp
        and event.signature not in prior
        for event in supporting
    )


def _has_correlated_alert(cause: Event, supporting: Sequence[Event]) -> bool:
    """An alert for the same service, firing after the cause."""
    return any(
        event.type is EventType.ALERT_FIRED
        and event.timestamp > cause.timestamp
        and (cause.service is None or event.service == cause.service)
        for event in supporting
    )
