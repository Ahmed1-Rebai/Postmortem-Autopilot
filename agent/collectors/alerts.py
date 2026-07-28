"""Alert collector — Alertmanager-shaped JSON.

Phase 1 reads a fixture; Phase 2 receives the same shape on a webhook and
Phase 4 queries the Alertmanager API. All three produce identical `RawRecord`s,
which is the point of the shape.

Only firing alerts become events. A resolved-only entry describes the recovery,
not the failure, and admitting it as a cause would let the linker propose that
the fix caused the outage.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from agent.collectors.base import CollectorError, RawRecord
from agent.collectors.logs import parse_timestamp
from agent.state import AttributeValue, EventType, Window

_LABEL_SERVICE_KEYS: Final[tuple[str, ...]] = ("service", "job", "app", "namespace")


class AlertCollector:
    """Reads an Alertmanager-shaped JSON file."""

    name = "alerts"

    def __init__(self, fixture: Path, source: str = "alertmanager") -> None:
        self._fixture = fixture
        self.source = source

    def collect(self, window: Window, service: str | None) -> Sequence[RawRecord]:
        if not self._fixture.is_file():
            raise CollectorError(f"alerts fixture not found: {self._fixture}")
        try:
            payload = json.loads(self._fixture.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectorError(f"cannot read {self._fixture}: {exc}") from exc

        records: list[RawRecord] = []
        for alert in _iter_alerts(payload):
            record = self._parse_alert(alert)
            if record is None:
                continue
            if not window.contains(record.timestamp):
                continue
            if service and record.service_hint and record.service_hint != service:
                continue
            records.append(record)
        return records

    def _parse_alert(self, alert: dict[str, Any]) -> RawRecord | None:
        status = str(alert.get("status", "firing")).lower()
        if status == "resolved" and not alert.get("startsAt"):
            return None

        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            return None

        started = parse_timestamp(alert.get("startsAt"))
        if started is None:
            return None

        rule_name = str(labels.get("alertname") or alert.get("name") or "").strip()
        if not rule_name:
            return None

        summary = str(
            annotations.get("summary") or annotations.get("description") or rule_name
        ).strip()
        service_hint = _service_from_labels(labels)

        attributes: dict[str, AttributeValue] = {
            "rule_name": rule_name,
            "alert_severity": str(labels.get("severity") or "unknown"),
            "status": status,
        }
        resolved = parse_timestamp(alert.get("endsAt"))
        if resolved is not None:
            attributes["resolved_at"] = resolved.isoformat()

        # Labels are the alert's identity, so they belong in the node — but
        # flattened, since the graph stores primitives.
        for key, value in labels.items():
            if key in {"alertname", "severity"}:
                continue
            attributes[f"label_{key}"] = str(value)

        return RawRecord(
            source=self.source,
            native_id=f"alert:{rule_name}:{int(started.timestamp())}",
            type=EventType.ALERT_FIRED,
            timestamp=started,
            message=f"{rule_name}: {summary}" if summary != rule_name else rule_name,
            service_hint=service_hint,
            attributes=attributes,
            raw=alert,
        )


def _iter_alerts(payload: object) -> list[dict[str, Any]]:
    """Accept both the webhook envelope and a bare list of alerts."""
    if isinstance(payload, dict):
        alerts = payload.get("alerts")
        if isinstance(alerts, list):
            return [item for item in alerts if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _service_from_labels(labels: dict[str, Any]) -> str | None:
    for key in _LABEL_SERVICE_KEYS:
        value = labels.get(key)
        if value:
            return str(value)
    return None
