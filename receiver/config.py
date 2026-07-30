"""Receiver config — one file, same rule as `agent/config.py`: no `os.getenv`
scattered through the receiver's modules.

This is a genuinely separate config from the pipeline's. The receiver never
imports `agent.config` — it has its own runtime (FastAPI/uvicorn/kubernetes-
client, no LangGraph, no Neo4j driver) and its own deployable, on purpose
(docs/05: "a pipeline crash can't take down the thing that accepts alerts").
Sharing a config module would blur that boundary even with separate
Dockerfiles.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ReceiverConfigError(RuntimeError):
    """Config is missing or malformed. Fails startup, not the first request."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ReceiverConfigError(f"{name} is not set")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ReceiverConfigError(f"{name}={raw!r} is not an integer") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ReceiverConfigError(f"{name}={raw!r} is not a number") from exc


@dataclass(frozen=True, slots=True)
class ReceiverConfig:
    #: Compared against the webhook's `X-Webhook-Secret` header. Required —
    #: an unauthenticated Job-creation endpoint reachable on the cluster
    #: network is not a narrow attack surface, it's an open one (docs/05).
    webhook_secret: str
    valkey_url: str
    namespace: str
    #: The image the receiver stamps into each Job it creates. Driven from
    #: the same values.yaml key as job-template.yaml's own image, by
    #: convention rather than by a shared mechanism — see
    #: k3s/helm/postmortem-autopilot/templates/job-template.yaml's docstring
    #: comment on why these can't literally share code.
    pipeline_image: str
    pipeline_image_pull_policy: str
    #: Padding added on both sides of an alert's [startsAt, endsAt] window.
    #: Deliberately not CAUSAL_WINDOW_MINUTES — that's the linker's
    #: temporal-proximity threshold inside the graph, a different concept
    #: from "how much slack to give evidence collection around an alert".
    window_pad_minutes: int
    #: How long a (fingerprint, startsAt) dedup key blocks a repeat Job.
    dedup_ttl_seconds: int
    job_backoff_limit: int
    job_ttl_seconds_after_finished: int

    # -- everything below is copied onto each Job this receiver creates,
    # sourced from the same values.yaml keys job-template.yaml renders from
    # (see receiver-deployment.yaml) — one values file, two independent
    # consumers, kept in sync by convention. See jobs.py's module docstring.
    llm_provider: str
    analyst_model: str
    writer_model: str
    neo4j_uri: str
    neo4j_user: str
    citation_coverage_threshold: float
    max_validation_retries: int
    causal_window_minutes: int
    anthropic_api_key_configured: bool
    openrouter_api_key_configured: bool
    #: `None` means don't push (matches `agent.config.Config.pushgateway_url`)
    #: — set once the observability chart is installed.
    pushgateway_url: str | None

    def __post_init__(self) -> None:
        if self.window_pad_minutes <= 0:
            raise ReceiverConfigError("INCIDENT_WINDOW_PAD_MINUTES must be > 0")
        if self.dedup_ttl_seconds <= 0:
            raise ReceiverConfigError("DEDUP_TTL_SECONDS must be > 0")


def load_config() -> ReceiverConfig:
    return ReceiverConfig(
        webhook_secret=_require("WEBHOOK_SECRET"),
        valkey_url=os.environ.get("VALKEY_URL", "redis://localhost:6379/0"),
        namespace=os.environ.get("PIPELINE_NAMESPACE", "postmortem"),
        pipeline_image=_require("PIPELINE_IMAGE"),
        pipeline_image_pull_policy=os.environ.get(
            "PIPELINE_IMAGE_PULL_POLICY", "IfNotPresent"
        ),
        window_pad_minutes=_int("INCIDENT_WINDOW_PAD_MINUTES", 30),
        dedup_ttl_seconds=_int("DEDUP_TTL_SECONDS", 21600),  # 6h, per docs/05
        job_backoff_limit=_int("JOB_BACKOFF_LIMIT", 2),
        job_ttl_seconds_after_finished=_int("JOB_TTL_SECONDS_AFTER_FINISHED", 3600),
        llm_provider=os.environ.get("LLM_PROVIDER", "mock"),
        analyst_model=os.environ.get("ANALYST_MODEL", "mock"),
        writer_model=os.environ.get("WRITER_MODEL", "mock"),
        neo4j_uri=_require("NEO4J_URI"),
        neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
        citation_coverage_threshold=_float("CITATION_COVERAGE_THRESHOLD", 0.95),
        max_validation_retries=_int("MAX_VALIDATION_RETRIES", 2),
        causal_window_minutes=_int("CAUSAL_WINDOW_MINUTES", 15),
        anthropic_api_key_configured=bool(
            os.environ.get("ANTHROPIC_API_KEY_CONFIGURED")
        ),
        openrouter_api_key_configured=bool(
            os.environ.get("OPENROUTER_API_KEY_CONFIGURED")
        ),
        pushgateway_url=os.environ.get("PUSHGATEWAY_URL") or None,
    )
