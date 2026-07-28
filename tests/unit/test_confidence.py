"""The confidence model, as an expected-value table.

docs/06 ranks this second only to signatures: it is the number the whole
product hangs on, and it is pure, so exhaustive testing is cheap.

Both worked examples from docs/03 are pinned here. If either drifts, the
documentation and the code disagree about what a score means.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.confidence import (
    SignalSet,
    available_signals,
    band_for,
    compute_confidence,
    contradiction_penalty,
    signals_for,
)
from agent.config import SIGNAL_NAMES, ConfidenceConfig
from agent.state import Event, EventType

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)


@pytest.fixture
def config() -> ConfidenceConfig:
    """The weights as shipped in config/confidence.yaml."""
    return ConfidenceConfig(
        weights={
            "change_path_overlap": 0.30,
            "log_signature_match": 0.25,
            "metric_correlation": 0.20,
            "temporal_proximity": 0.15,
            "human_confirmation": 0.10,
        },
        contradiction_per_event=0.15,
        contradiction_cap=0.40,
        band_likely=0.75,
        band_plausible=0.40,
    )


ALL_BUT_CHAT = frozenset(SIGNAL_NAMES - {"human_confirmation"})


# ---------------------------------------------------------------------------
# the worked examples from docs/03 — these are the contract with the docs
# ---------------------------------------------------------------------------
def test_worked_example_h1_deploy_dropped_db_pool_max(config: ConfidenceConfig):
    """All four available signals fire, no contradictions → 1.00, likely."""
    signals = SignalSet(fired=ALL_BUT_CHAT, available=ALL_BUT_CHAT)
    result = compute_confidence(signals, contradicting_events=0, config=config)

    assert result.denominator == pytest.approx(0.90)
    assert result.numerator == pytest.approx(0.90)
    assert result.confidence == pytest.approx(1.00)
    assert result.band == "likely"


def test_worked_example_h2_upstream_gateway_latency(config: ConfidenceConfig):
    """Two signals fire, one contradiction → 0.29, tentative."""
    signals = SignalSet(
        fired=frozenset({"log_signature_match", "temporal_proximity"}),
        available=ALL_BUT_CHAT,
    )
    result = compute_confidence(signals, contradicting_events=1, config=config)

    assert result.numerator == pytest.approx(0.40)
    assert result.denominator == pytest.approx(0.90)
    assert result.raw_support == pytest.approx(0.4444, abs=1e-4)
    assert result.contradiction_penalty == pytest.approx(0.15)
    assert round(result.confidence, 2) == pytest.approx(0.29)
    assert result.band == "tentative"


def test_the_two_worked_examples_are_well_separated(config: ConfidenceConfig):
    """Confidence separation is what makes the score informative rather than
    decorative — if wrong answers scored like right ones, it would be noise."""
    h1 = compute_confidence(SignalSet(ALL_BUT_CHAT, ALL_BUT_CHAT), 0, config).confidence
    h2 = compute_confidence(
        SignalSet(
            frozenset({"log_signature_match", "temporal_proximity"}), ALL_BUT_CHAT
        ),
        1,
        config,
    ).confidence
    assert h1 - h2 > 0.20


# ---------------------------------------------------------------------------
# the score table
# ---------------------------------------------------------------------------
TABLE: list[tuple[str, set[str], set[str], int, float, str]] = [
    # name, fired, available, contradictions, expected confidence, band
    (
        "everything fires, all sources",
        set(SIGNAL_NAMES),
        set(SIGNAL_NAMES),
        0,
        1.00,
        "likely",
    ),
    (
        "nothing fires",
        set(),
        set(SIGNAL_NAMES),
        0,
        0.00,
        "tentative",
    ),
    (
        "only temporal proximity — the weakest possible case",
        {"temporal_proximity"},
        set(SIGNAL_NAMES),
        0,
        0.15,
        "tentative",
    ),
    (
        "change path alone, all sources",
        {"change_path_overlap"},
        set(SIGNAL_NAMES),
        0,
        0.30,
        "tentative",
    ),
    (
        "change path + temporal, all sources",
        {"change_path_overlap", "temporal_proximity"},
        set(SIGNAL_NAMES),
        0,
        0.45,
        "plausible",
    ),
    (
        "change path + logs + temporal, all sources",
        {"change_path_overlap", "log_signature_match", "temporal_proximity"},
        set(SIGNAL_NAMES),
        0,
        0.70,
        "plausible",
    ),
    (
        "same signals but chat unavailable — denominator shrinks, score rises",
        {"change_path_overlap", "log_signature_match", "temporal_proximity"},
        ALL_BUT_CHAT,
        0,
        0.7778,
        "likely",
    ),
    (
        "alerts only available, alert fires",
        {"metric_correlation", "temporal_proximity"},
        {"metric_correlation", "temporal_proximity"},
        0,
        1.00,
        "likely",
    ),
    (
        "strong support, one contradiction",
        set(SIGNAL_NAMES),
        set(SIGNAL_NAMES),
        1,
        0.85,
        "likely",
    ),
    (
        "strong support, three contradictions — capped at 0.40",
        set(SIGNAL_NAMES),
        set(SIGNAL_NAMES),
        3,
        0.60,
        "plausible",
    ),
    (
        "strong support, ten contradictions — still capped, never zeroed",
        set(SIGNAL_NAMES),
        set(SIGNAL_NAMES),
        10,
        0.60,
        "plausible",
    ),
    (
        "weak support and contradictions clamp at zero, not below",
        {"temporal_proximity"},
        set(SIGNAL_NAMES),
        5,
        0.00,
        "tentative",
    ),
]


@pytest.mark.parametrize(
    ("name", "fired", "available", "contradictions", "expected", "band"),
    TABLE,
    ids=[row[0] for row in TABLE],
)
def test_score_table(
    config: ConfidenceConfig,
    name: str,
    fired: set[str],
    available: set[str],
    contradictions: int,
    expected: float,
    band: str,
):
    result = compute_confidence(
        SignalSet(frozenset(fired), frozenset(available)), contradictions, config
    )
    assert result.confidence == pytest.approx(expected, abs=1e-4)
    assert result.band == band


# ---------------------------------------------------------------------------
# availability — the fix for "missing source permanently caps every score"
# ---------------------------------------------------------------------------
def test_unavailable_source_is_excluded_not_penalized(config: ConfidenceConfig):
    """The same evidence must not score lower just because chat isn't wired
    up. That was the flaw in the naive corroborating/total model."""
    fired = {"change_path_overlap", "log_signature_match", "temporal_proximity"}
    with_chat = compute_confidence(
        SignalSet(frozenset(fired), frozenset(SIGNAL_NAMES)), 0, config
    )
    without_chat = compute_confidence(
        SignalSet(frozenset(fired), ALL_BUT_CHAT), 0, config
    )
    assert without_chat.confidence > with_chat.confidence
    assert "human_confirmation" in without_chat.excluded


def test_no_available_signals_scores_zero(config: ConfidenceConfig):
    """Nothing was collectible, so there is nothing to be confident from."""
    result = compute_confidence(SignalSet(frozenset(), frozenset()), 0, config)
    assert result.confidence == 0.0
    assert result.band == "tentative"


def test_a_signal_that_fired_without_its_source_cannot_inflate(
    config: ConfidenceConfig,
):
    """Defensive: a caller mislabelling availability must not produce > 1.0."""
    result = compute_confidence(
        SignalSet(frozenset(SIGNAL_NAMES), frozenset({"temporal_proximity"})),
        0,
        config,
    )
    assert result.confidence == pytest.approx(1.0)
    assert result.numerator == pytest.approx(0.15)


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (["logs", "git", "alerts"], ALL_BUT_CHAT),
        (["logs"], {"log_signature_match", "temporal_proximity"}),
        ([], {"temporal_proximity"}),
        (
            ["logs", "git", "alerts", "chat"],
            SIGNAL_NAMES,
        ),
    ],
)
def test_available_signals_from_sources(sources: list[str], expected: set[str]):
    assert available_signals(sources) == frozenset(expected)


def test_temporal_proximity_is_always_available():
    """It needs only timestamps, which every event has."""
    assert "temporal_proximity" in available_signals([])


def test_a_failed_collector_shrinks_the_denominator(config: ConfidenceConfig):
    """A degraded run yields lower certainty, not a penalized score."""
    degraded = available_signals(["logs", "git"])  # alerts collector failed
    assert "metric_correlation" not in degraded
    result = compute_confidence(
        SignalSet(frozenset({"change_path_overlap"}), degraded), 0, config
    )
    assert result.denominator == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# bands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (1.00, "likely"),
        (0.76, "likely"),
        (0.75, "likely"),  # boundary is inclusive
        (0.7499, "plausible"),
        (0.50, "plausible"),
        (0.40, "plausible"),  # boundary is inclusive
        (0.3999, "tentative"),
        (0.00, "tentative"),
    ],
)
def test_band_boundaries(config: ConfidenceConfig, confidence: float, expected: str):
    assert band_for(confidence, config) == expected


def test_band_and_number_never_disagree(config: ConfidenceConfig):
    """The band is derived from the rounded score, so a document cannot show
    0.75 next to the word "plausible"."""
    for contradictions in range(0, 4):
        for fired in ({"change_path_overlap"}, set(SIGNAL_NAMES), set()):
            result = compute_confidence(
                SignalSet(frozenset(fired), frozenset(SIGNAL_NAMES)),
                contradictions,
                config,
            )
            assert result.band == band_for(result.confidence, config)


# ---------------------------------------------------------------------------
# contradiction penalty
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, 0.0), (1, 0.15), (2, 0.30), (3, 0.40), (4, 0.40), (100, 0.40)],
)
def test_contradiction_penalty_is_capped(
    config: ConfidenceConfig, count: int, expected: float
):
    assert contradiction_penalty(count, config) == pytest.approx(expected)


def test_negative_contradictions_are_rejected(config: ConfidenceConfig):
    with pytest.raises(ValueError, match="cannot be negative"):
        contradiction_penalty(-1, config)


def test_contradiction_can_demote_but_never_zero_a_strong_hypothesis(
    config: ConfidenceConfig,
):
    """A hypothesis with full support and many contradictions is still in play,
    and that ambiguity is what should reach the reader."""
    result = compute_confidence(
        SignalSet(frozenset(SIGNAL_NAMES), frozenset(SIGNAL_NAMES)), 99, config
    )
    assert result.confidence == pytest.approx(0.60)
    assert result.band != "tentative"


# ---------------------------------------------------------------------------
# decomposability
# ---------------------------------------------------------------------------
def test_every_score_decomposes_into_its_signals(config: ConfidenceConfig):
    """A number the reader cannot take apart is one they must take on faith."""
    signals = SignalSet(
        frozenset({"change_path_overlap", "temporal_proximity"}),
        frozenset(SIGNAL_NAMES),
    )
    result = compute_confidence(signals, 0, config)
    assert result.contributions == {
        "change_path_overlap": 0.30,
        "temporal_proximity": 0.15,
    }
    assert sum(result.contributions.values()) == pytest.approx(result.numerator)
    assert "change_path_overlap=+0.30" in result.explain()


def test_explain_mentions_contradictions(config: ConfidenceConfig):
    result = compute_confidence(
        SignalSet(frozenset({"temporal_proximity"}), frozenset(SIGNAL_NAMES)), 2, config
    )
    assert "contradiction" in result.explain()


def test_unknown_signal_names_are_rejected():
    with pytest.raises(ValueError, match="unknown signals"):
        SignalSet(frozenset({"vibes"}), frozenset(SIGNAL_NAMES))


def test_scoring_is_deterministic(config: ConfidenceConfig):
    signals = SignalSet(frozenset({"change_path_overlap"}), frozenset(SIGNAL_NAMES))
    assert (
        compute_confidence(signals, 1, config).confidence
        == compute_confidence(signals, 1, config).confidence
    )


# ---------------------------------------------------------------------------
# deriving which signals fired from real evidence
# ---------------------------------------------------------------------------
def an_event(
    event_id: str,
    kind: EventType,
    offset: int,
    service: str | None = "checkout",
    signature: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        type=kind,
        timestamp=START + timedelta(seconds=offset),
        source="test",
        summary=event_id,
        service=service,
        signature=signature,
    )


def test_signals_derived_from_a_textbook_incident():
    cause = an_event("commit", EventType.COMMIT, 0)
    log = an_event("log", EventType.LOG_ERROR, 40, signature="pool exhausted")
    alert = an_event("alert", EventType.ALERT_FIRED, 120)
    events = [cause, log, alert]

    fired = signals_for(
        cause, [log, alert], events, ["temporal_proximity", "change_path_overlap"]
    )
    assert fired == {
        "temporal_proximity",
        "change_path_overlap",
        "log_signature_match",
        "metric_correlation",
    }


def test_preexisting_error_does_not_corroborate():
    """An error that was already happening is not evidence that this change
    started it — otherwise background noise corroborates every candidate."""
    old = an_event("old", EventType.LOG_ERROR, -60, signature="pool exhausted")
    cause = an_event("commit", EventType.COMMIT, 0)
    new = an_event("new", EventType.LOG_ERROR, 40, signature="pool exhausted")

    fired = signals_for(cause, [new], [old, cause, new], ["temporal_proximity"])
    assert "log_signature_match" not in fired


def test_a_genuinely_new_signature_does_corroborate():
    old = an_event("old", EventType.LOG_ERROR, -60, signature="disk full")
    cause = an_event("commit", EventType.COMMIT, 0)
    new = an_event("new", EventType.LOG_ERROR, 40, signature="pool exhausted")

    fired = signals_for(cause, [new], [old, cause, new], ["temporal_proximity"])
    assert "log_signature_match" in fired


def test_alert_for_another_service_does_not_correlate():
    cause = an_event("commit", EventType.COMMIT, 0, service="checkout")
    alert = an_event("alert", EventType.ALERT_FIRED, 60, service="inventory")
    fired = signals_for(cause, [alert], [cause, alert], [])
    assert "metric_correlation" not in fired


def test_alert_before_the_cause_does_not_correlate():
    cause = an_event("commit", EventType.COMMIT, 100)
    alert = an_event("alert", EventType.ALERT_FIRED, 10)
    fired = signals_for(cause, [alert], [cause, alert], [])
    assert "metric_correlation" not in fired


def test_chat_message_gives_human_confirmation():
    cause = an_event("commit", EventType.COMMIT, 0)
    chat = an_event("chat", EventType.CHAT_MESSAGE, 60)
    fired = signals_for(cause, [chat], [cause, chat], [])
    assert "human_confirmation" in fired


def test_service_overlap_is_not_a_confidence_signal():
    """It helps propose a candidate, but scoring it as well would double-count
    evidence `change_path_overlap` already reflects."""
    cause = an_event("commit", EventType.COMMIT, 0)
    log = an_event("log", EventType.LOG_ERROR, 40, signature="boom")
    fired = signals_for(
        cause, [log], [cause, log], ["temporal_proximity", "service_overlap"]
    )
    assert "service_overlap" not in fired
    assert fired <= SIGNAL_NAMES


def test_derived_signals_feed_the_score(config: ConfidenceConfig):
    """The two halves compose: evidence in, decomposable number out."""
    cause = an_event("commit", EventType.COMMIT, 0)
    log = an_event("log", EventType.LOG_ERROR, 40, signature="pool exhausted")
    alert = an_event("alert", EventType.ALERT_FIRED, 120)
    events = [cause, log, alert]

    fired = signals_for(
        cause, [log, alert], events, ["temporal_proximity", "change_path_overlap"]
    )
    result = compute_confidence(
        SignalSet(fired, available_signals(["logs", "git", "alerts"])), 0, config
    )
    assert result.confidence == pytest.approx(1.0)
    assert result.band == "likely"
