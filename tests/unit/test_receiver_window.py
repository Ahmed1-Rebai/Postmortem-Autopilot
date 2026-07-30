"""Deriving an incident from a webhook payload — pure functions, tested as an
expected-value table, same standard the pipeline's own pure logic gets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from receiver.models import AlertmanagerAlert, AlertmanagerWebhook
from receiver.window import NoResolvedAlertsError, derive_incident

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)
END = datetime(2026, 3, 12, 14, 30, tzinfo=UTC)
UNRESOLVED = "0001-01-01T00:00:00Z"


def an_alert(
    *,
    status: str = "resolved",
    starts_at: datetime = START,
    ends_at: str | datetime = END,
    alertname: str = "HighErrorRate",
    severity: str = "page",
    service: str | None = "checkout",
    fingerprint: str = "abc123",
) -> AlertmanagerAlert:
    labels = {"alertname": alertname, "severity": severity}
    if service:
        labels["service"] = service
    return AlertmanagerAlert.model_validate(
        {
            "status": status,
            "labels": labels,
            "annotations": {"summary": "error rate above 5%"},
            "startsAt": starts_at.isoformat(),
            "endsAt": ends_at.isoformat() if isinstance(ends_at, datetime) else ends_at,
            "fingerprint": fingerprint,
        }
    )


def a_webhook(
    alerts: list[AlertmanagerAlert], group_key: str = '{}:{alertname="x"}'
) -> AlertmanagerWebhook:
    return AlertmanagerWebhook(groupKey=group_key, status="resolved", alerts=alerts)


# ---------------------------------------------------------------------------
# window math
# ---------------------------------------------------------------------------
def test_window_pads_both_sides():
    derived = derive_incident(a_webhook([an_alert()]), pad_minutes=10)
    assert derived.incident.start_time == START - timedelta(minutes=10)
    assert derived.incident.end_time == END + timedelta(minutes=10)


def test_window_spans_the_earliest_start_and_latest_end_across_alerts():
    early = an_alert(starts_at=START, ends_at=START.replace(minute=20))
    late = an_alert(starts_at=START.replace(minute=5), ends_at=END)
    derived = derive_incident(a_webhook([early, late]), pad_minutes=0)
    assert derived.incident.start_time == START  # earliest startsAt
    assert derived.incident.end_time == END  # latest endsAt


def test_firing_only_alerts_raise_no_resolved_alerts():
    """Alertmanager sends `firing` updates too; only a resolved incident has
    a complete evidence window."""
    webhook = a_webhook([an_alert(status="firing", ends_at=UNRESOLVED)])
    with pytest.raises(NoResolvedAlertsError):
        derive_incident(webhook, pad_minutes=10)


def test_a_mix_of_firing_and_resolved_uses_only_the_resolved_ones():
    firing = an_alert(status="firing", ends_at=UNRESOLVED, fingerprint="f1")
    resolved = an_alert(status="resolved", fingerprint="f2")
    derived = derive_incident(a_webhook([firing, resolved]), pad_minutes=0)
    assert derived.incident.start_time == START
    assert derived.incident.end_time == END


def test_zero_pad_is_allowed():
    derived = derive_incident(a_webhook([an_alert()]), pad_minutes=0)
    assert derived.incident.start_time == START
    assert derived.incident.end_time == END


# ---------------------------------------------------------------------------
# incident id — content-derived, invariant 5's idempotency story
# ---------------------------------------------------------------------------
def test_incident_id_is_deterministic():
    a = derive_incident(a_webhook([an_alert()]), pad_minutes=10)
    b = derive_incident(a_webhook([an_alert()]), pad_minutes=10)
    assert a.incident.id == b.incident.id


def test_incident_id_varies_with_group_key():
    a = derive_incident(a_webhook([an_alert()], group_key="g1"), pad_minutes=10)
    b = derive_incident(a_webhook([an_alert()], group_key="g2"), pad_minutes=10)
    assert a.incident.id != b.incident.id


def test_incident_id_varies_with_window_start():
    a = derive_incident(a_webhook([an_alert(starts_at=START)]), pad_minutes=0)
    b = derive_incident(
        a_webhook([an_alert(starts_at=START.replace(minute=1))]), pad_minutes=0
    )
    assert a.incident.id != b.incident.id


def test_a_retried_webhook_produces_the_same_incident():
    """Alertmanager retries a webhook it didn't get a fast 2xx for. The retry
    must resolve to the same incident, not a second one — the same reason
    event IDs are content-derived (invariant 5)."""
    webhook = a_webhook([an_alert()])
    first = derive_incident(webhook, pad_minutes=15)
    second = derive_incident(webhook, pad_minutes=15)
    assert first.incident.id == second.incident.id
    assert first.dedup_key == second.dedup_key


# ---------------------------------------------------------------------------
# title / severity
# ---------------------------------------------------------------------------
def test_title_lists_distinct_alert_names_sorted():
    a = an_alert(alertname="HighErrorRate")
    b = an_alert(alertname="DiskSpaceLow", fingerprint="f2")
    derived = derive_incident(a_webhook([a, b]), pad_minutes=0)
    assert derived.incident.title == "DiskSpaceLow, HighErrorRate"


def test_title_deduplicates_repeated_alert_names():
    a = an_alert(alertname="HighErrorRate", fingerprint="f1")
    b = an_alert(alertname="HighErrorRate", fingerprint="f2")
    derived = derive_incident(a_webhook([a, b]), pad_minutes=0)
    assert derived.incident.title == "HighErrorRate"


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        (["warning", "critical"], "critical"),
        (["info", "warning"], "warning"),
        (["page", "info"], "page"),
        (["unknown"], "unknown"),
        ([], "unknown"),
    ],
)
def test_severity_arbitration(severities: list[str], expected: str):
    alerts = [
        an_alert(severity=sev, fingerprint=f"f{i}") for i, sev in enumerate(severities)
    ]
    if not alerts:
        alerts = [an_alert(severity="unknown")]
    derived = derive_incident(a_webhook(alerts), pad_minutes=0)
    assert derived.incident.severity == expected


# ---------------------------------------------------------------------------
# dedup key
# ---------------------------------------------------------------------------
def test_dedup_key_is_the_group_key_and_window_start():
    derived = derive_incident(
        a_webhook([an_alert()], group_key="mygroup"), pad_minutes=5
    )
    assert (
        derived.dedup_key == f"dedup:mygroup:{derived.incident.start_time.isoformat()}"
    )
