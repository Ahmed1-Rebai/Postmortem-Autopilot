"""Unit tests for the domain model.

These types are where several invariants are enforced structurally rather than
by convention, so the tests are mostly about what the constructors *refuse*.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent.state import (
    CandidateLink,
    CitationCheck,
    Complaint,
    ComplaintKind,
    Event,
    EventType,
    Hypothesis,
    Incident,
    RunReport,
    SourceFailure,
    ValidationReport,
    Window,
    make_event_id,
    make_hypothesis_id,
)

TS = datetime(2026, 3, 12, 14, 2, 11, tzinfo=UTC)


def an_event(**overrides: object) -> Event:
    kwargs: dict[str, object] = {
        "id": "a3f9c1e2b4d6f001",
        "type": EventType.LOG_ERROR,
        "timestamp": TS,
        "source": "loki",
        "summary": "checkout returned 500",
    }
    kwargs.update(overrides)
    return Event(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# content-derived IDs (invariant 5)
# ---------------------------------------------------------------------------
def test_event_id_is_deterministic():
    assert make_event_id("loki", "line-42") == make_event_id("loki", "line-42")


def test_event_id_is_16_hex_chars():
    event_id = make_event_id("github", "a3f9c1e2")
    assert len(event_id) == 16
    assert all(c in "0123456789abcdef" for c in event_id)


def test_event_id_separator_prevents_boundary_collision():
    """Without a separator, ("ab","c") and ("a","bc") would hash identically —
    two different events silently becoming one node."""
    assert make_event_id("ab", "c") != make_event_id("a", "bc")


def test_event_id_varies_with_source():
    assert make_event_id("loki", "x") != make_event_id("alertmanager", "x")


@pytest.mark.parametrize(("source", "native"), [("", "x"), ("loki", ""), ("", "")])
def test_event_id_rejects_empty_parts(source: str, native: str):
    with pytest.raises(ValueError, match="needs both"):
        make_event_id(source, native)


def test_hypothesis_id_is_namespaced_by_incident():
    """The same statement about two incidents must not collide onto one node."""
    a = make_hypothesis_id("INC-0001", "deploy dropped DB_POOL_MAX")
    b = make_hypothesis_id("INC-0007", "deploy dropped DB_POOL_MAX")
    assert a != b
    assert a.startswith("hyp:")


# ---------------------------------------------------------------------------
# timezone-awareness (CLAUDE.md: naive datetimes are a bug)
# ---------------------------------------------------------------------------
def test_event_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        an_event(timestamp=datetime(2026, 3, 12, 14, 2, 11))  # noqa: DTZ001


def test_incident_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        Incident(
            id="INC-0001",
            title="checkout 500s",
            start_time=datetime(2026, 3, 12, 14, 0),  # noqa: DTZ001
            end_time=TS,
        )


def test_window_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        Window(datetime(2026, 3, 12, 14, 0), TS)  # noqa: DTZ001


def test_non_utc_aware_timestamps_are_accepted():
    """Aware is the requirement, not UTC specifically — a collector reporting
    +02:00 is fine because the instant is unambiguous."""
    berlin = timezone(timedelta(hours=2))
    event = an_event(timestamp=datetime(2026, 3, 12, 16, 2, 11, tzinfo=berlin))
    assert event.timestamp.utcoffset() == timedelta(hours=2)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------
def test_event_attributes_may_not_shadow_base_properties():
    """A subtype property overwriting `timestamp` would corrupt the timeline
    and every citation check against it."""
    with pytest.raises(ValueError, match="may not shadow"):
        an_event(attributes={"timestamp": "nope"})


def test_event_attributes_reject_nested_structures():
    with pytest.raises(ValueError, match="only primitives"):
        an_event(attributes={"labels": {"severity": "page"}})


def test_event_attributes_accept_primitives_and_tuples():
    event = an_event(
        attributes={
            "commit_sha": "a3f9c1e2",
            "files_changed": ("config/database.py", "app/pool.py"),
            "resolved": False,
            "duration": 1.5,
            "owner": None,
        }
    )
    assert event.attributes["files_changed"] == ("config/database.py", "app/pool.py")


def test_event_rejects_zero_count():
    with pytest.raises(ValueError, match="count must be >= 1"):
        an_event(count=0)


def test_event_rejects_empty_id():
    with pytest.raises(ValueError, match="must not be empty"):
        an_event(id="")


def test_raw_json_survives_unserializable_values():
    """Losing the node over an odd payload would be worse than a lossy debug
    field, so serialization degrades rather than raising."""
    event = an_event(raw={"when": TS, "msg": "boom"})
    assert "boom" in event.raw_json()


def test_events_compare_by_identity_not_payload():
    """`raw` and `attributes` are excluded from equality — two collectors
    seeing the same event with different debug payloads are the same event."""
    assert an_event(raw={"a": 1}) == an_event(raw={"b": 2})


# ---------------------------------------------------------------------------
# Incident / Window
# ---------------------------------------------------------------------------
def test_incident_rejects_backwards_window():
    with pytest.raises(ValueError, match="before it starts"):
        Incident(
            id="INC-0001",
            title="t",
            start_time=TS,
            end_time=TS - timedelta(minutes=5),
        )


def test_window_contains_is_inclusive():
    window = Window(TS, TS + timedelta(minutes=15))
    assert window.contains(TS)
    assert window.contains(TS + timedelta(minutes=15))
    assert not window.contains(TS - timedelta(seconds=1))


# ---------------------------------------------------------------------------
# CandidateLink
# ---------------------------------------------------------------------------
def test_candidate_link_rejects_self_causation():
    with pytest.raises(ValueError, match="cannot cause itself"):
        CandidateLink("e1", "e1", ("temporal_proximity",), 30)


def test_candidate_link_requires_at_least_one_heuristic():
    """An edge nothing voted for carries no signal for the confidence model."""
    with pytest.raises(ValueError, match="no heuristics"):
        CandidateLink("e1", "e2", (), 30)


def test_candidate_link_rejects_unknown_heuristic():
    with pytest.raises(ValueError, match="unknown heuristics"):
        CandidateLink("e1", "e2", ("vibes",), 30)


# ---------------------------------------------------------------------------
# Hypothesis
# ---------------------------------------------------------------------------
def a_hypothesis(**overrides: object) -> Hypothesis:
    kwargs: dict[str, object] = {
        "id": "hyp:abc",
        "rank": 1,
        "statement": "deploy dropped DB_POOL_MAX",
        "supporting_event_ids": ("e1", "e2"),
        "contradicting_event_ids": (),
        "confidence": 0.94,
        "band": "likely",
    }
    kwargs.update(overrides)
    return Hypothesis(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_hypothesis_rejects_out_of_range_confidence(confidence: float):
    with pytest.raises(ValueError, match="outside"):
        a_hypothesis(confidence=confidence)


def test_hypothesis_rejects_rank_zero():
    with pytest.raises(ValueError, match="rank must be >= 1"):
        a_hypothesis(rank=0)


def test_event_cannot_both_support_and_contradict():
    with pytest.raises(ValueError, match="cannot both support and contradict"):
        a_hypothesis(supporting_event_ids=("e1",), contradicting_event_ids=("e1",))


def test_empty_contradicting_evidence_is_allowed_but_explicit():
    """The field is required and may be empty — what it may never be is unset."""
    assert a_hypothesis(contradicting_event_ids=()).contradicting_event_ids == ()


# ---------------------------------------------------------------------------
# ValidationReport — invariant 3
# ---------------------------------------------------------------------------
def a_report(**overrides: object) -> ValidationReport:
    kwargs: dict[str, object] = {
        "incident_id": "INC-0001",
        "factual_sentences": 20,
        "cited_sentences": 20,
        "citations_total": 25,
        "citations_hallucinated": 0,
        "coverage_threshold": 0.95,
    }
    kwargs.update(overrides)
    return ValidationReport(**kwargs)  # type: ignore[arg-type]


def test_full_coverage_no_hallucinations_passes():
    assert a_report().passed
    assert a_report().coverage == pytest.approx(1.0)


def test_one_hallucinated_citation_fails_despite_perfect_coverage():
    """Invariant 3: hallucinated citations are not a quality metric to trade
    against coverage. One is a hard failure."""
    report = a_report(citations_hallucinated=1)
    assert report.coverage == pytest.approx(1.0)
    assert not report.passed


def test_coverage_below_threshold_fails():
    report = a_report(cited_sentences=18)
    assert report.coverage == pytest.approx(0.9)
    assert not report.passed


def test_coverage_exactly_at_threshold_passes():
    """≥, not >. A document at exactly 0.95 must not be rejected."""
    report = a_report(factual_sentences=100, cited_sentences=95)
    assert report.coverage == pytest.approx(0.95)
    assert report.passed


def test_document_with_no_factual_sentences_is_vacuously_covered():
    """It makes no claims, so it cannot make an uncited one."""
    report = a_report(factual_sentences=0, cited_sentences=0, citations_total=0)
    assert report.coverage == pytest.approx(1.0)
    assert report.passed


def test_hallucination_rate_is_zero_when_nothing_is_cited():
    assert a_report(citations_total=0).hallucination_rate == pytest.approx(0.0)


def test_hallucination_rate_is_a_ratio_of_citations():
    report = a_report(citations_total=25, citations_hallucinated=5)
    assert report.hallucination_rate == pytest.approx(0.2)


def test_complaints_are_structured_not_prose():
    complaint = Complaint(
        kind=ComplaintKind.HALLUCINATED_ID,
        detail="cited 9f2b0000 which does not exist",
        sentence_index=12,
        cited_id="9f2b0000",
    )
    report = a_report(citations_hallucinated=1, complaints=(complaint,))
    assert report.complaints[0].kind is ComplaintKind.HALLUCINATED_ID
    assert report.complaints[0].sentence_index == 12


# ---------------------------------------------------------------------------
# RunReport
# ---------------------------------------------------------------------------
def test_run_report_duration_and_success():
    report = RunReport(
        incident_id="INC-0001",
        started_at=TS,
        finished_at=TS + timedelta(seconds=34),
        sources_used=("logs", "git"),
        sources_failed=(SourceFailure("alerts", "connection refused"),),
        event_count=50,
        candidate_count=12,
        hypothesis_count=2,
        retry_count=0,
        token_usage={"analyst": 4000, "writer": 8000},
        validation=a_report(),
    )
    assert report.duration_seconds == pytest.approx(34.0)
    assert report.succeeded
    assert report.sources_failed[0].source == "alerts"


def test_run_report_without_validation_has_not_succeeded():
    """No validation result means the run did not get far enough to claim one."""
    report = RunReport(
        incident_id="INC-0001",
        started_at=TS,
        finished_at=TS,
        sources_used=(),
        sources_failed=(),
        event_count=0,
        candidate_count=0,
        hypothesis_count=0,
        retry_count=0,
        token_usage={},
        validation=None,
    )
    assert not report.succeeded


# ---------------------------------------------------------------------------
# CitationCheck
# ---------------------------------------------------------------------------
def test_citation_check_defaults_to_no_timestamp():
    check = CitationCheck(cited_id="deadbeef", valid=False)
    assert check.actual_timestamp is None
