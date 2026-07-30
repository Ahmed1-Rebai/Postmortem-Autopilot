"""Building the ConfigMap/Job manifests and the create-then-link sequence —
tested against fake `BatchV1ApiLike`/`CoreV1ApiLike` doubles, per the
`Protocol`-based injection `receiver/jobs.py` was built for. No real
Kubernetes API involved: this is about manifest shape and call ordering, not
cluster behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent.state import Incident
from receiver.config import ReceiverConfig
from receiver.jobs import IncidentJobCreator, build_configmap, build_job


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


def an_incident() -> Incident:
    return Incident(
        id="INC-ALERT-abc123def456",
        title="HighErrorRate",
        start_time=datetime(2026, 3, 12, 14, 0, tzinfo=UTC),
        end_time=datetime(2026, 3, 12, 14, 30, tzinfo=UTC),
        severity="page",
        status="closed",
    )


# ---------------------------------------------------------------------------
# build_configmap
# ---------------------------------------------------------------------------
def test_configmap_data_round_trips_meta_and_alerts():
    incident = an_incident()
    alerts_payload = {"alerts": [{"status": "resolved"}]}
    configmap = build_configmap(incident, alerts_payload, name="pm-inc-abc")

    assert configmap["kind"] == "ConfigMap"
    assert configmap["metadata"]["name"] == "pm-inc-abc"
    assert json.loads(configmap["data"]["alerts.json"]) == alerts_payload

    import yaml

    meta = yaml.safe_load(configmap["data"]["meta.yaml"])
    assert meta == {
        "id": incident.id,
        "title": incident.title,
        "start": incident.start_time.isoformat(),
        "end": incident.end_time.isoformat(),
        "severity": incident.severity,
        "status": incident.status,
    }


def test_configmap_has_no_logs_or_repo_keys():
    """Only meta.yaml + alerts.json — LogCollector/GitCollector degrade to
    SourceFailure on a missing mount path, which is the designed behavior,
    not something this module works around."""
    configmap = build_configmap(an_incident(), {"alerts": []}, name="pm-x")
    assert set(configmap["data"].keys()) == {"meta.yaml", "alerts.json"}


# ---------------------------------------------------------------------------
# build_job
# ---------------------------------------------------------------------------
def test_job_name_matches_configmap_name():
    job = build_job(an_incident(), configmap_name="pm-inc-abc", config=a_config())
    assert job["metadata"]["name"] == "pm-inc-abc"


def test_job_mounts_the_named_configmap_read_only():
    job = build_job(an_incident(), configmap_name="pm-inc-abc", config=a_config())
    pod_spec = job["spec"]["template"]["spec"]
    incident_volume = next(v for v in pod_spec["volumes"] if v["name"] == "incident")
    assert incident_volume["configMap"]["name"] == "pm-inc-abc"
    incident_mount = next(
        m for m in pod_spec["containers"][0]["volumeMounts"] if m["name"] == "incident"
    )
    assert incident_mount["readOnly"] is True
    assert incident_mount["mountPath"] == "/incident"


def test_job_uses_the_configured_pipeline_image():
    config = a_config(pipeline_image="myregistry/postmortem:v3")
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "myregistry/postmortem:v3"


def test_job_security_context_is_fully_hardened():
    """The pipeline's own container is fully controlled — full hardening,
    unlike the Neo4j/Valkey upstream images this project could not lock
    down the same way (documented Checkpoint 4 finding)."""
    job = build_job(an_incident(), configmap_name="pm-x", config=a_config())
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    container_sc = pod_spec["containers"][0]["securityContext"]
    assert container_sc["allowPrivilegeEscalation"] is False
    assert container_sc["readOnlyRootFilesystem"] is True
    assert container_sc["capabilities"]["drop"] == ["ALL"]


def test_job_env_omits_api_key_vars_when_not_configured():
    config = a_config(
        anthropic_api_key_configured=False, openrouter_api_key_configured=False
    )
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    names = {e["name"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "ANTHROPIC_API_KEY" not in names
    assert "OPENROUTER_API_KEY" not in names


def test_job_env_omits_pushgateway_url_when_not_configured():
    config = a_config(pushgateway_url=None)
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    names = {e["name"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "PUSHGATEWAY_URL" not in names


def test_job_env_includes_pushgateway_url_when_configured():
    config = a_config(pushgateway_url="http://pushgateway.observability.svc:9091")
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    env = {
        e["name"]: e["value"]
        for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }
    assert env["PUSHGATEWAY_URL"] == "http://pushgateway.observability.svc:9091"


def test_job_env_includes_anthropic_key_ref_when_configured():
    config = a_config(anthropic_api_key_configured=True)
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    entry = next(e for e in env if e["name"] == "ANTHROPIC_API_KEY")
    assert entry["valueFrom"]["secretKeyRef"] == {
        "name": "postmortem-secrets",
        "key": "anthropic-api-key",
    }


def test_job_env_includes_openrouter_key_ref_when_configured():
    config = a_config(openrouter_api_key_configured=True)
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    entry = next(e for e in env if e["name"] == "OPENROUTER_API_KEY")
    assert entry["valueFrom"]["secretKeyRef"] == {
        "name": "postmortem-secrets",
        "key": "openrouter-api-key",
    }


def test_job_env_carries_reasoning_and_verification_settings():
    config = a_config(
        citation_coverage_threshold=0.9,
        max_validation_retries=3,
        causal_window_minutes=20,
    )
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    env = {
        e["name"]: e["value"]
        for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }
    assert env["CITATION_COVERAGE_THRESHOLD"] == "0.9"
    assert env["MAX_VALIDATION_RETRIES"] == "3"
    assert env["CAUSAL_WINDOW_MINUTES"] == "20"


def test_job_backoff_and_ttl_come_from_config():
    config = a_config(job_backoff_limit=5, job_ttl_seconds_after_finished=999)
    job = build_job(an_incident(), configmap_name="pm-x", config=config)
    assert job["spec"]["backoffLimit"] == 5
    assert job["spec"]["ttlSecondsAfterFinished"] == 999


def test_job_args_point_at_the_mounted_incident_directory():
    job = build_job(an_incident(), configmap_name="pm-x", config=a_config())
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == ["run", "--incident", "/incident"]


# ---------------------------------------------------------------------------
# _object_name — DNS-label safety
# ---------------------------------------------------------------------------
def test_object_name_is_lowercased_and_prefixed():
    from receiver.jobs import _object_name

    assert _object_name("INC-ALERT-abc123def456") == "pm-inc-alert-abc123def456"


# ---------------------------------------------------------------------------
# IncidentJobCreator.create — call sequence against fake clients
# ---------------------------------------------------------------------------
@dataclass
class _Metadata:
    name: str
    uid: str


@dataclass
class _CreatedJob:
    metadata: _Metadata


class FakeBatchApi:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> Any:
        self.calls.append({"namespace": namespace, "body": body})
        name = body["metadata"]["name"]
        return _CreatedJob(metadata=_Metadata(name=name, uid="job-uid-1"))


class FakeCoreApi:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.patch_calls: list[dict[str, Any]] = []

    def create_namespaced_config_map(self, namespace: str, body: dict[str, Any]) -> Any:
        self.create_calls.append({"namespace": namespace, "body": body})
        name = body["metadata"]["name"]
        return _CreatedJob(metadata=_Metadata(name=name, uid="cm-uid-1"))

    def patch_namespaced_config_map(
        self, name: str, namespace: str, body: dict[str, Any]
    ) -> Any:
        self.patch_calls.append({"name": name, "namespace": namespace, "body": body})
        return None


def test_create_writes_configmap_then_job_then_patches_owner_reference():
    batch_api = FakeBatchApi()
    core_api = FakeCoreApi()
    creator = IncidentJobCreator(
        batch_api=batch_api, core_api=core_api, config=a_config()
    )

    job_name = creator.create(an_incident(), {"alerts": []})

    assert job_name == "pm-inc-alert-abc123def456"
    assert len(core_api.create_calls) == 1
    assert len(batch_api.calls) == 1
    assert len(core_api.patch_calls) == 1

    # ConfigMap and Job share the same name/namespace.
    assert core_api.create_calls[0]["body"]["metadata"]["name"] == job_name
    assert batch_api.calls[0]["body"]["metadata"]["name"] == job_name
    assert batch_api.calls[0]["namespace"] == "postmortem"

    # Owner reference patch points at the created Job's uid.
    patch_body = core_api.patch_calls[0]["body"]
    owner_refs = patch_body["metadata"]["ownerReferences"]
    assert len(owner_refs) == 1
    assert owner_refs[0]["uid"] == "job-uid-1"
    assert owner_refs[0]["name"] == job_name
    assert owner_refs[0]["kind"] == "Job"
    assert core_api.patch_calls[0]["name"] == job_name


def test_create_uses_the_configured_namespace_everywhere():
    batch_api = FakeBatchApi()
    core_api = FakeCoreApi()
    config = a_config(namespace="custom-ns")
    creator = IncidentJobCreator(batch_api=batch_api, core_api=core_api, config=config)

    creator.create(an_incident(), {"alerts": []})

    assert core_api.create_calls[0]["namespace"] == "custom-ns"
    assert batch_api.calls[0]["namespace"] == "custom-ns"
    assert core_api.patch_calls[0]["namespace"] == "custom-ns"
