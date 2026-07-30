"""FastAPI route tests via `TestClient`, with the cluster-dependent
singletons (`get_config`/`get_dedup`/`get_job_creator`) swapped out through
`app.dependency_overrides` — exactly the seam those functions were converted
to lazy `lru_cache` functions to make possible (see receiver/main.py's
module docstring on why they can't be built at import time).

No real Valkey, no real Kubernetes API: this is about routing, auth, dedup
branching, and response shape.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.state import Incident
from receiver.config import ReceiverConfig
from receiver.main import app, get_config, get_dedup, get_job_creator


def a_config(**overrides: Any) -> ReceiverConfig:
    defaults: dict[str, Any] = {
        "webhook_secret": "s3cr3t",
        "valkey_url": "redis://valkey:6379/0",
        "namespace": "postmortem",
        "pipeline_image": "postmortem-autopilot:dev",
        "pipeline_image_pull_policy": "IfNotPresent",
        "window_pad_minutes": 30,
        "dedup_ttl_seconds": 21600,
        "job_backoff_limit": 2,
        "job_ttl_seconds_after_finished": 3600,
        "llm_provider": "mock",
        "analyst_model": "mock",
        "writer_model": "mock",
        "neo4j_uri": "bolt://neo4j:7687",
        "neo4j_user": "neo4j",
        "citation_coverage_threshold": 0.95,
        "max_validation_retries": 2,
        "causal_window_minutes": 15,
        "anthropic_api_key_configured": False,
        "openrouter_api_key_configured": False,
        "pushgateway_url": None,
    }
    defaults.update(overrides)
    return ReceiverConfig(**defaults)


class FakeDedup:
    def __init__(self, *, claims: bool = True, ping_ok: bool = True) -> None:
        self._claims = claims
        self._ping_ok = ping_ok
        self.claimed_keys: list[str] = []

    def claim(self, key: str) -> bool:
        self.claimed_keys.append(key)
        return self._claims

    @property
    def client(self) -> FakeDedup:
        return self

    def ping(self) -> bool:
        if not self._ping_ok:
            raise ConnectionError("valkey down")
        return True


class FakeJobCreator:
    def __init__(self) -> None:
        self.created: list[tuple[Incident, dict[str, Any]]] = []

    def create(self, incident: Incident, alerts_payload: dict[str, Any]) -> str:
        self.created.append((incident, alerts_payload))
        return f"pm-{incident.id.lower()}"


RESOLVED_ALERT_BODY = {
    "groupKey": '{}:{alertname="HighErrorRate"}',
    "status": "resolved",
    "alerts": [
        {
            "status": "resolved",
            "labels": {"alertname": "HighErrorRate", "severity": "page"},
            "annotations": {},
            "startsAt": "2026-03-12T14:00:00Z",
            "endsAt": "2026-03-12T14:30:00Z",
            "fingerprint": "abc123",
        }
    ],
}

FIRING_ONLY_BODY = {
    "groupKey": '{}:{alertname="HighErrorRate"}',
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighErrorRate", "severity": "page"},
            "annotations": {},
            "startsAt": "2026-03-12T14:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "abc123",
        }
    ],
}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# health / readiness — no auth required
# ---------------------------------------------------------------------------
def test_healthz_does_not_touch_dependencies(client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_ok_when_valkey_reachable(client: TestClient):
    app.dependency_overrides[get_dedup] = lambda: FakeDedup(ping_ok=True)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_503_when_valkey_unreachable(client: TestClient):
    app.dependency_overrides[get_dedup] = lambda: FakeDedup(ping_ok=False)
    response = client.get("/readyz")
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def test_hooks_401_without_secret_header(client: TestClient):
    app.dependency_overrides[get_config] = lambda: a_config()
    response = client.post("/hooks/alertmanager", json=RESOLVED_ALERT_BODY)
    assert response.status_code == 401


def test_hooks_401_with_wrong_secret(client: TestClient):
    app.dependency_overrides[get_config] = lambda: a_config()
    response = client.post(
        "/hooks/alertmanager",
        json=RESOLVED_ALERT_BODY,
        headers={"X-Webhook-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_hooks_accepts_bearer_token_auth(client: TestClient):
    """The mechanism Alertmanager's own http_config.bearer_token actually
    sends — it can't send an arbitrary custom header name."""
    config = a_config()
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_dedup] = lambda: FakeDedup()
    app.dependency_overrides[get_job_creator] = lambda: FakeJobCreator()

    response = client.post(
        "/hooks/alertmanager",
        json=RESOLVED_ALERT_BODY,
        headers={"Authorization": f"Bearer {config.webhook_secret}"},
    )
    assert response.status_code == 202


def test_hooks_401_with_wrong_bearer_token(client: TestClient):
    app.dependency_overrides[get_config] = lambda: a_config()
    response = client.post(
        "/hooks/alertmanager",
        json=RESOLVED_ALERT_BODY,
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# hooks/alertmanager behavior
# ---------------------------------------------------------------------------
def test_hooks_no_op_when_no_resolved_alerts(client: TestClient):
    config = a_config()
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_dedup] = lambda: FakeDedup()
    app.dependency_overrides[get_job_creator] = lambda: FakeJobCreator()

    response = client.post(
        "/hooks/alertmanager",
        json=FIRING_ONLY_BODY,
        headers={"X-Webhook-Secret": config.webhook_secret},
    )
    assert response.status_code == 202
    assert response.json()["action"] == "no-op"


def test_hooks_deduped_when_already_claimed(client: TestClient):
    config = a_config()
    job_creator = FakeJobCreator()
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_dedup] = lambda: FakeDedup(claims=False)
    app.dependency_overrides[get_job_creator] = lambda: job_creator

    response = client.post(
        "/hooks/alertmanager",
        json=RESOLVED_ALERT_BODY,
        headers={"X-Webhook-Secret": config.webhook_secret},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["action"] == "deduped"
    assert job_creator.created == []


def test_hooks_creates_a_job_on_first_resolved_alert(client: TestClient):
    config = a_config()
    job_creator = FakeJobCreator()
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_dedup] = lambda: FakeDedup(claims=True)
    app.dependency_overrides[get_job_creator] = lambda: job_creator

    response = client.post(
        "/hooks/alertmanager",
        json=RESOLVED_ALERT_BODY,
        headers={"X-Webhook-Secret": config.webhook_secret},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["action"] == "created"
    assert body["job"].startswith("pm-inc-alert-")
    assert len(job_creator.created) == 1

    incident, alerts_payload = job_creator.created[0]
    assert incident.title == "HighErrorRate"
    assert alerts_payload == RESOLVED_ALERT_BODY


def test_hooks_passes_the_raw_body_verbatim_to_the_configmap(client: TestClient):
    """The receiver writes the raw webhook JSON into alerts.json unchanged —
    AlertCollector already unwraps this exact envelope shape."""
    config = a_config()
    job_creator = FakeJobCreator()
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_dedup] = lambda: FakeDedup(claims=True)
    app.dependency_overrides[get_job_creator] = lambda: job_creator

    extra_body = {**RESOLVED_ALERT_BODY, "receiver": "postmortem-autopilot"}
    client.post(
        "/hooks/alertmanager",
        json=extra_body,
        headers={"X-Webhook-Secret": config.webhook_secret},
    )
    _, alerts_payload = job_creator.created[0]
    assert alerts_payload == extra_body
