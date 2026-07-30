"""The Alertmanager webhook payload, as Pydantic models.

Deliberately not a reuse of `agent.collectors.alerts`'s parsing — that module's
job is producing `RawRecord`s for the evidence plane; this one's is deriving a
window and a dedup key and creating a Job. Different concerns, and the overlap
is small enough (a handful of fields) that duplicating it is cheaper than
coupling two modules that would otherwise have no reason to change together.

What *is* shared, byte-for-byte: the receiver writes the raw webhook body into
the synthesized incident's `alerts.json` unchanged. `AlertCollector._iter_alerts`
already unwraps a `{"alerts": [...]}` envelope — which is exactly Alertmanager's
own webhook shape — so nothing needs re-encoding.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

#: Alertmanager's sentinel for "not resolved" — a Go zero-value time,
#: `time.Time{}`, serialized. Never a real endsAt.
_UNRESOLVED_SENTINEL = "0001-01-01T00:00:00Z"


class AlertmanagerAlert(BaseModel):
    """One alert within a webhook payload. Extra fields (generatorURL, etc.)
    are ignored, not rejected — the receiver only needs a handful of them, and
    Alertmanager's payload shape has drifted across versions before."""

    model_config = ConfigDict(extra="ignore")

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime
    fingerprint: str = ""

    @property
    def is_resolved(self) -> bool:
        """Only resolved alerts carry a real `endsAt` — and only a resolved
        alert has a complete evidence window to collect over. `send_resolved:
        true` in the Alertmanager receiver config (docs/05) is what makes this
        endpoint see resolved alerts at all."""
        return self.status.lower() == "resolved" and (
            self.endsAt.isoformat().replace("+00:00", "Z") != _UNRESOLVED_SENTINEL
        )


class AlertmanagerWebhook(BaseModel):
    """The full POST body. `groupKey` is Alertmanager's own identifier for
    "these alerts were grouped together" — a sturdier dedup anchor than any
    single alert's fingerprint, since one webhook call can carry several."""

    model_config = ConfigDict(extra="ignore")

    groupKey: str
    status: str = "firing"
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)

    @property
    def resolved_alerts(self) -> list[AlertmanagerAlert]:
        return [alert for alert in self.alerts if alert.is_resolved]
