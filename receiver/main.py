"""The webhook receiver: `/hooks/alertmanager`, `/healthz`, `/readyz`.

The receiver only creates Jobs — it never runs the pipeline in-process
(docs/05). That keeps it tiny, always-responsive to probes, and means a
pipeline crash can't take down the thing that accepts alerts. It never talks
to Neo4j at all: only to Valkey (dedup) and the Kubernetes API (Job creation).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from receiver.config import ReceiverConfig, load_config
from receiver.dedup import Deduplicator
from receiver.jobs import IncidentJobCreator
from receiver.models import AlertmanagerWebhook
from receiver.window import NoResolvedAlertsError, derive_incident

logger = logging.getLogger("receiver")

app = FastAPI(title="postmortem-autopilot-receiver")


def _load_kube_config() -> None:
    """In-cluster by default; falls back to a local kubeconfig for `uvicorn
    --reload` development against a k3d cluster from outside the pod."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


# -- process-lifetime singletons, built lazily on first use ----------------
# Not at import time. `agent/config.py` has the same `get_config()` +
# `lru_cache` shape for the same reason: importing this module must not
# require WEBHOOK_SECRET/PIPELINE_IMAGE/NEO4J_URI to be set or a Kubernetes
# context to be loadable — a test process has neither, and the mock-provider
# principle applies here too (CLAUDE.md: a code path CI exercises must not
# gain an unconditional external dependency). FastAPI route handlers call
# these through `Depends(...)`, and tests override that dependency entirely
# via `app.dependency_overrides`, so the real, cluster-dependent versions
# never run in a test process at all.
@lru_cache(maxsize=1)
def get_config() -> ReceiverConfig:
    return load_config()


@lru_cache(maxsize=1)
def get_dedup() -> Deduplicator:
    config = get_config()
    return Deduplicator.from_url(config.valkey_url, config.dedup_ttl_seconds)


@lru_cache(maxsize=1)
def get_job_creator() -> IncidentJobCreator:
    _load_kube_config()
    return IncidentJobCreator(
        batch_api=k8s_client.BatchV1Api(),
        core_api=k8s_client.CoreV1Api(),
        config=get_config(),
    )


def verify_webhook_secret(
    x_webhook_secret: str = Header(default=""),
    config: ReceiverConfig = Depends(get_config),
) -> None:
    """Reachable from the cluster network — this is not decoration
    (docs/05: "should not be an open Job-creation API"). Compared with `!=`
    rather than a constant-time comparator: the secret is a shared,
    infrequently-rotated cluster value, not a per-user credential, and the
    threat model here is "keep this off the open internet", not "resist a
    timing side-channel from another pod on the same cluster network"."""
    if not x_webhook_secret or x_webhook_secret != config.webhook_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook secret")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. No dependency checks — a Valkey blip
    should not make the kubelet restart a perfectly good receiver pod."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz(dedup: Deduplicator = Depends(get_dedup)) -> dict[str, str]:
    """Readiness: can this pod actually do its job — reach Valkey. Kubernetes
    stops routing traffic here on failure without restarting the pod."""
    try:
        dedup.client.ping()
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"valkey unreachable: {exc}"
        ) from exc
    return {"status": "ready"}


@app.post("/hooks/alertmanager", status_code=status.HTTP_202_ACCEPTED)
async def hooks_alertmanager(
    request: Request,
    _: None = Depends(verify_webhook_secret),
    dedup: Deduplicator = Depends(get_dedup),
    job_creator: IncidentJobCreator = Depends(get_job_creator),
    config: ReceiverConfig = Depends(get_config),
) -> dict[str, Any]:
    raw_body = await request.json()
    webhook = AlertmanagerWebhook.model_validate(raw_body)

    try:
        derived = derive_incident(webhook, pad_minutes=config.window_pad_minutes)
    except NoResolvedAlertsError:
        # Alertmanager sends `firing` updates too (send_resolved also fires
        # on the initial alert). Correctly accepted, correctly a no-op: only
        # a resolved incident has a complete evidence window to collect over.
        return {"status": "accepted", "action": "no-op", "reason": "not resolved"}

    if not dedup.claim(derived.dedup_key):
        logger.info("deduped %s (key=%s)", derived.incident.id, derived.dedup_key)
        return {
            "status": "accepted",
            "action": "deduped",
            "incident_id": derived.incident.id,
        }

    job_name = job_creator.create(derived.incident, raw_body)
    logger.info("created job %s for incident %s", job_name, derived.incident.id)
    return {
        "status": "accepted",
        "action": "created",
        "incident_id": derived.incident.id,
        "job": job_name,
    }
