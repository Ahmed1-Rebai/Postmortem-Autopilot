"""`RawRecord` → `Event`, and the signature collapsing that bounds token cost."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.collectors.base import RawRecord
from agent.normalize.events import normalize_records, to_event
from agent.normalize.services import ServiceCanonicalizer
from agent.state import EventType, make_event_id

START = datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)


@pytest.fixture
def canonicalizer() -> ServiceCanonicalizer:
    return ServiceCanonicalizer.from_config({"checkout": ["co"]}, ["-svc", "-service"])


def a_log(index: int, message: str, service: str | None = "checkout-svc") -> RawRecord:
    return RawRecord(
        source="logs",
        native_id=f"app.log:{index}",
        type=EventType.LOG_ERROR,
        timestamp=START + timedelta(seconds=index),
        message=message,
        service_hint=service,
        attributes={"level": "error"},
    )


# ---------------------------------------------------------------------------
# single-record normalization
# ---------------------------------------------------------------------------
def test_service_is_canonicalized(canonicalizer: ServiceCanonicalizer):
    event = to_event(a_log(1, "pool exhausted"), canonicalizer)
    assert event.service == "checkout"


def test_log_errors_get_a_signature(canonicalizer: ServiceCanonicalizer):
    event = to_event(a_log(1, "ConnectionError: pool exhausted (32/32)"), canonicalizer)
    assert event.signature == "connectionerror: pool exhausted (<num>/<num>)"


def test_commits_get_no_signature(canonicalizer: ServiceCanonicalizer):
    """A commit message has no failure mode to fingerprint; inventing one would
    put noise in the signature index and in every incident fingerprint."""
    record = RawRecord(
        source="git",
        native_id="commit:a3f9c1e2",
        type=EventType.COMMIT,
        timestamp=START,
        message="remove DB_POOL_MAX",
    )
    assert to_event(record, canonicalizer).signature is None


def test_alerts_get_a_signature(canonicalizer: ServiceCanonicalizer):
    record = RawRecord(
        source="alertmanager",
        native_id="alert:HighErrorRate:1",
        type=EventType.ALERT_FIRED,
        timestamp=START,
        message="HighErrorRate: error rate above 5% for 5m",
    )
    assert to_event(record, canonicalizer).signature is not None


def test_event_id_is_derived_from_source_and_native_id(
    canonicalizer: ServiceCanonicalizer,
):
    record = RawRecord(
        source="git",
        native_id="commit:a3f9c1e2",
        type=EventType.COMMIT,
        timestamp=START,
        message="x",
    )
    assert to_event(record, canonicalizer).id == make_event_id("git", "commit:a3f9c1e2")


def test_attributes_pass_through(canonicalizer: ServiceCanonicalizer):
    event = to_event(a_log(1, "boom"), canonicalizer)
    assert event.attributes["level"] == "error"


# ---------------------------------------------------------------------------
# collapsing — what bounds token cost
# ---------------------------------------------------------------------------
def test_repeated_failures_collapse_to_one_event(
    canonicalizer: ServiceCanonicalizer,
):
    records = [
        a_log(i, f"ConnectionError: pool exhausted ({i}/{i})") for i in range(1, 401)
    ]
    events = normalize_records(records, canonicalizer)

    assert len(events) == 1, "400 lines of one failure mode must be one node"
    assert events[0].count == 400


def test_collapsed_event_keeps_the_earliest_timestamp(
    canonicalizer: ServiceCanonicalizer,
):
    """The first occurrence is the causally interesting one; using the last
    would place the effect after events it actually preceded."""
    records = [a_log(i, "pool exhausted") for i in (30, 5, 17)]
    events = normalize_records(records, canonicalizer)
    assert events[0].timestamp == START + timedelta(seconds=5)


def test_distinct_failure_modes_stay_distinct(canonicalizer: ServiceCanonicalizer):
    records = [
        a_log(1, "ConnectionError: pool exhausted (1/1)"),
        a_log(2, "TimeoutError: upstream did not respond"),
        a_log(3, "ConnectionError: pool exhausted (2/2)"),
    ]
    events = normalize_records(records, canonicalizer)
    assert len(events) == 2


def test_same_failure_in_different_services_does_not_collapse(
    canonicalizer: ServiceCanonicalizer,
):
    """Collapsing across services would merge two outages into one node and
    manufacture corroboration for a cause that only touched one of them."""
    records = [
        a_log(1, "pool exhausted", service="checkout"),
        a_log(2, "pool exhausted", service="inventory"),
    ]
    events = normalize_records(records, canonicalizer)
    assert len(events) == 2


def test_collapsing_is_stable_across_runs(canonicalizer: ServiceCanonicalizer):
    """The collapsed node's ID comes from the signature, not from any one line,
    so a rerun that sees a different number of lines lands on the same node —
    which is what keeps citations stable."""
    first = normalize_records(
        [a_log(i, f"pool exhausted ({i}/{i})") for i in range(1, 11)], canonicalizer
    )
    second = normalize_records(
        [a_log(i, f"pool exhausted ({i}/{i})") for i in range(1, 51)], canonicalizer
    )
    assert first[0].id == second[0].id
    assert first[0].count == 10
    assert second[0].count == 50


def test_a_single_occurrence_is_also_rekeyed(canonicalizer: ServiceCanonicalizer):
    """One line now and two lines later must be the same node, not two."""
    once = normalize_records([a_log(1, "pool exhausted")], canonicalizer)
    twice = normalize_records(
        [a_log(1, "pool exhausted"), a_log(2, "pool exhausted")], canonicalizer
    )
    assert once[0].id == twice[0].id


def test_non_log_events_are_never_collapsed(canonicalizer: ServiceCanonicalizer):
    """Two deploys are two deploys even with identical messages."""
    records = [
        RawRecord(
            source="argo",
            native_id=f"deploy-{i}",
            type=EventType.DEPLOY,
            timestamp=START + timedelta(minutes=i),
            message="checkout rolled out",
            service_hint="checkout",
        )
        for i in (1, 2)
    ]
    assert len(normalize_records(records, canonicalizer)) == 2


def test_output_is_sorted_by_timestamp(canonicalizer: ServiceCanonicalizer):
    records = [
        a_log(30, "timeouterror: upstream gone"),
        a_log(5, "pool exhausted"),
        RawRecord(
            source="git",
            native_id="commit:abc",
            type=EventType.COMMIT,
            timestamp=START + timedelta(seconds=1),
            message="the commit",
        ),
    ]
    events = normalize_records(records, canonicalizer)
    assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)


def test_empty_input_is_empty_output(canonicalizer: ServiceCanonicalizer):
    assert normalize_records([], canonicalizer) == []


def test_log_without_a_usable_signature_is_not_collapsed(
    canonicalizer: ServiceCanonicalizer,
):
    """A message that fingerprints to nothing has no signature to group on, so
    it must pass through rather than collapse with every other blank one."""
    records = [a_log(1, "ERROR"), a_log(2, "ERROR")]
    events = normalize_records(records, canonicalizer)
    assert len(events) == 2
    assert all(event.signature is None for event in events)
