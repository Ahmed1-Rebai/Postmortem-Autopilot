"""Deriving an incident from a batch of resolved alerts.

Pure functions — no Valkey, no Kubernetes client — so the window math and the
incident-id/dedup-key derivation are unit-testable with an expected-value
table, the same standard CLAUDE.md sets for the pipeline's own pure logic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.state import Incident
from receiver.models import AlertmanagerAlert, AlertmanagerWebhook


class NoResolvedAlertsError(ValueError):
    """A webhook payload with nothing resolved in it. Not an error condition
    for the caller — Alertmanager sends `firing` updates too, and those are
    correctly accepted (2xx) with no Job created, per docs/05: only a
    resolved incident has a complete evidence window."""


@dataclass(frozen=True, slots=True)
class DerivedIncident:
    incident: Incident
    dedup_key: str


def derive_incident(
    webhook: AlertmanagerWebhook, *, pad_minutes: int
) -> DerivedIncident:
    """One incident per webhook call: the window spans every resolved alert's
    [startsAt, endsAt], padded on both sides."""
    resolved = webhook.resolved_alerts
    if not resolved:
        raise NoResolvedAlertsError(webhook.groupKey)

    pad = timedelta(minutes=pad_minutes)
    start = min(alert.startsAt for alert in resolved) - pad
    end = max(alert.endsAt for alert in resolved) + pad

    incident_id = _incident_id(webhook.groupKey, start)
    title = _title(resolved)

    incident = Incident(
        id=incident_id,
        title=title,
        start_time=start,
        end_time=end,
        severity=_severity(resolved),
        status="closed",
    )
    return DerivedIncident(
        incident=incident,
        dedup_key=f"dedup:{webhook.groupKey}:{start.isoformat()}",
    )


def _incident_id(group_key: str, start: datetime) -> str:
    """Content-derived, same idempotency story as `make_event_id` (invariant
    5) — Alertmanager retries a webhook it didn't get a fast 2xx for, and a
    retry must resolve to the same incident, not a second one."""
    digest = hashlib.sha256(f"{group_key}:{start.isoformat()}".encode())
    return f"INC-ALERT-{digest.hexdigest()[:12]}"


def _title(alerts: list[AlertmanagerAlert]) -> str:
    names = sorted({alert.labels.get("alertname", "unknown") for alert in alerts})
    return ", ".join(names)


def _severity(alerts: list[AlertmanagerAlert]) -> str:
    """The most severe label wins arbitration, in the usual page > warning >
    info order Alertmanager configs use."""
    ranked = {"critical": 0, "page": 0, "warning": 1, "info": 2}
    severities = [alert.labels.get("severity", "unknown") for alert in alerts]
    ranked_present = [s for s in severities if s in ranked]
    if not ranked_present:
        return severities[0] if severities else "unknown"
    return min(ranked_present, key=lambda s: ranked[s])
