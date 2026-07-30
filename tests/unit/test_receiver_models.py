"""The Alertmanager webhook payload shape — validated against a fixture
modeled on Alertmanager's own real notification format, not an invented one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from receiver.models import AlertmanagerAlert, AlertmanagerWebhook

#: A real Alertmanager webhook body's shape (v4), trimmed to what matters.
REAL_WEBHOOK = {
    "version": "4",
    "groupKey": '{}:{alertname="HighErrorRate"}',
    "truncatedAlerts": 0,
    "status": "resolved",
    "receiver": "postmortem-autopilot",
    "groupLabels": {"alertname": "HighErrorRate"},
    "commonLabels": {"alertname": "HighErrorRate", "service": "checkout"},
    "commonAnnotations": {"summary": "error rate above 5%"},
    "externalURL": "http://alertmanager.observability.svc:9093",
    "alerts": [
        {
            "status": "resolved",
            "labels": {
                "alertname": "HighErrorRate",
                "service": "checkout",
                "severity": "page",
            },
            "annotations": {"summary": "error rate above 5% for 5m"},
            "startsAt": "2026-03-12T14:02:11Z",
            "endsAt": "2026-03-12T14:30:00Z",
            "generatorURL": "http://prometheus/graph?g0.expr=...",
            "fingerprint": "a3f9c1e2b4d6f001",
        }
    ],
}


def test_parses_a_real_shaped_webhook_body():
    webhook = AlertmanagerWebhook.model_validate(REAL_WEBHOOK)
    assert webhook.groupKey == '{}:{alertname="HighErrorRate"}'
    assert len(webhook.alerts) == 1
    assert webhook.alerts[0].fingerprint == "a3f9c1e2b4d6f001"


def test_extra_top_level_fields_are_ignored_not_rejected():
    """Alertmanager's payload shape has drifted across versions before —
    rejecting on an unrecognized field would make an upgrade a receiver
    outage."""
    payload = {**REAL_WEBHOOK, "someFutureField": {"nested": True}}
    webhook = AlertmanagerWebhook.model_validate(payload)
    assert webhook.groupKey == REAL_WEBHOOK["groupKey"]


def test_extra_alert_fields_are_ignored():
    payload = {
        **REAL_WEBHOOK,
        "alerts": [{**REAL_WEBHOOK["alerts"][0], "silenceURL": "http://..."}],
    }
    webhook = AlertmanagerWebhook.model_validate(payload)
    assert webhook.alerts[0].fingerprint == "a3f9c1e2b4d6f001"


def test_missing_group_key_is_rejected():
    payload = {k: v for k, v in REAL_WEBHOOK.items() if k != "groupKey"}
    with pytest.raises(ValidationError):
        AlertmanagerWebhook.model_validate(payload)


def test_a_webhook_with_no_alerts_is_valid_but_empty():
    """Alertmanager can send a group-resolved notification with an empty
    alerts array in edge cases; this should not crash the endpoint."""
    payload = {**REAL_WEBHOOK, "alerts": []}
    webhook = AlertmanagerWebhook.model_validate(payload)
    assert webhook.resolved_alerts == []


# ---------------------------------------------------------------------------
# is_resolved — the unresolved sentinel
# ---------------------------------------------------------------------------
def test_resolved_status_with_a_real_endsat_is_resolved():
    alert = AlertmanagerAlert.model_validate(REAL_WEBHOOK["alerts"][0])
    assert alert.is_resolved


def test_firing_status_is_not_resolved_even_with_a_real_endsat():
    """status is the authoritative signal, not just endsAt's presence."""
    payload = {**REAL_WEBHOOK["alerts"][0], "status": "firing"}
    alert = AlertmanagerAlert.model_validate(payload)
    assert not alert.is_resolved


def test_the_zero_value_sentinel_is_not_resolved_even_with_status_resolved():
    """Alertmanager's Go zero-value time, serialized — this is what a firing
    (never-resolved) alert's endsAt actually looks like."""
    payload = {
        **REAL_WEBHOOK["alerts"][0],
        "status": "resolved",
        "endsAt": "0001-01-01T00:00:00Z",
    }
    alert = AlertmanagerAlert.model_validate(payload)
    assert not alert.is_resolved


def test_status_is_case_insensitive():
    payload = {**REAL_WEBHOOK["alerts"][0], "status": "RESOLVED"}
    alert = AlertmanagerAlert.model_validate(payload)
    assert alert.is_resolved


# ---------------------------------------------------------------------------
# resolved_alerts filter
# ---------------------------------------------------------------------------
def test_resolved_alerts_filters_correctly():
    firing = {
        **REAL_WEBHOOK["alerts"][0],
        "status": "firing",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": "firing-one",
    }
    resolved = REAL_WEBHOOK["alerts"][0]
    webhook = AlertmanagerWebhook.model_validate(
        {**REAL_WEBHOOK, "alerts": [firing, resolved]}
    )
    assert [a.fingerprint for a in webhook.resolved_alerts] == ["a3f9c1e2b4d6f001"]
