"""Turn a derived incident into a running pipeline Job.

Two Kubernetes objects, not one: a per-incident ConfigMap (`meta.yaml` +
`alerts.json`, mounted at `/incident`) and the Job that reads it. The
ConfigMap is created first, then owner-referenced to the Job once the Job
exists — so when `ttlSecondsAfterFinished` reaps the Job, Kubernetes' garbage
collector reaps the ConfigMap with it, for free, no separate cleanup CronJob.

This does not use `k3s/helm/postmortem-autopilot/templates/job-template.yaml`.
Nothing running in a pod can re-render a Helm chart it has no access to —
Helm templating is an install-time operation, not a cluster-runtime one. The
two are independent expressions of "what a pipeline Job looks like", kept in
sync by convention (both read the same values from `ReceiverConfig`, itself
populated from the chart's own `values.yaml` at Deployment time), not by a
shared mechanism. That's a real, accepted drift risk for a project this size,
not a hidden one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import yaml

from agent.state import Incident
from receiver.config import ReceiverConfig

#: Kept short and DNS-label-safe: Job/ConfigMap names derive from the
#: incident id, which k8s object names cap at 63 characters.
_NAME_PREFIX = "pm-"


class BatchV1ApiLike(Protocol):
    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> Any: ...


class CoreV1ApiLike(Protocol):
    def create_namespaced_config_map(
        self, namespace: str, body: dict[str, Any]
    ) -> Any: ...
    def patch_namespaced_config_map(
        self, name: str, namespace: str, body: dict[str, Any]
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class IncidentJobCreator:
    batch_api: BatchV1ApiLike
    core_api: CoreV1ApiLike
    config: ReceiverConfig

    def create(self, incident: Incident, alerts_payload: dict[str, Any]) -> str:
        """Create the ConfigMap, then the Job, then link them. Returns the
        Job's name."""
        job_name = _object_name(incident.id)
        configmap = build_configmap(incident, alerts_payload, name=job_name)
        self.core_api.create_namespaced_config_map(
            namespace=self.config.namespace, body=configmap
        )

        job = build_job(incident, configmap_name=job_name, config=self.config)
        created_job = self.batch_api.create_namespaced_job(
            namespace=self.config.namespace, body=job
        )

        self.core_api.patch_namespaced_config_map(
            name=job_name,
            namespace=self.config.namespace,
            body={"metadata": {"ownerReferences": [_owner_reference(created_job)]}},
        )
        return job_name


def build_configmap(
    incident: Incident, alerts_payload: dict[str, Any], *, name: str
) -> dict[str, Any]:
    """`meta.yaml` + `alerts.json`, matching exactly what `cli.load_incident_spec`
    and `AlertCollector` already expect — no new parsing contract invented.

    No `logs`/`repo` keys: a ConfigMap volume only materializes the keys it
    has, so `<mount>/logs` and `<mount>/repo` simply don't exist. `LogCollector`
    and `GitCollector` degrade to `SourceFailure` on a missing path — already
    the designed behavior (docs/07 case 05: evidence genuinely absent → say
    so, don't invent), not a new failure mode this module has to handle.
    """
    meta = {
        "id": incident.id,
        "title": incident.title,
        "start": incident.start_time.isoformat(),
        "end": incident.end_time.isoformat(),
        "severity": incident.severity,
        "status": incident.status,
    }
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name},
        "data": {
            "meta.yaml": yaml.safe_dump(meta, sort_keys=False),
            "alerts.json": json.dumps(alerts_payload),
        },
    }


def build_job(
    incident: Incident, *, configmap_name: str, config: ReceiverConfig
) -> dict[str, Any]:
    """The pod spec, deliberately structured like `job-template.yaml`'s —
    same volumes-for-HOME/OUTPUT_DIR-under-readOnlyRootFilesystem story, same
    security context — because it is describing the same container, just
    parameterized per-incident instead of pointing at a baked-in fixture."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": configmap_name},
        "spec": {
            "backoffLimit": config.job_backoff_limit,
            "ttlSecondsAfterFinished": config.job_ttl_seconds_after_finished,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "pipeline",
                            "image": config.pipeline_image,
                            "imagePullPolicy": config.pipeline_image_pull_policy,
                            "args": ["run", "--incident", "/incident"],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "env": _job_env(config),
                            "volumeMounts": [
                                {"name": "scratch", "mountPath": "/scratch"},
                                {
                                    "name": "incident",
                                    "mountPath": "/incident",
                                    "readOnly": True,
                                },
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "scratch", "emptyDir": {}},
                        {
                            "name": "incident",
                            "configMap": {"name": configmap_name},
                        },
                    ],
                }
            },
        },
    }


def _job_env(config: ReceiverConfig) -> list[dict[str, Any]]:
    env: list[dict[str, Any]] = [
        {"name": "LLM_PROVIDER", "value": config.llm_provider},
        {"name": "ANALYST_MODEL", "value": config.analyst_model},
        {"name": "WRITER_MODEL", "value": config.writer_model},
        {"name": "NEO4J_URI", "value": config.neo4j_uri},
        {"name": "NEO4J_USER", "value": config.neo4j_user},
        {
            "name": "NEO4J_PASSWORD",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "postmortem-secrets",
                    "key": "neo4j-password",
                }
            },
        },
        {
            "name": "CITATION_COVERAGE_THRESHOLD",
            "value": str(config.citation_coverage_threshold),
        },
        {"name": "MAX_VALIDATION_RETRIES", "value": str(config.max_validation_retries)},
        {"name": "CAUSAL_WINDOW_MINUTES", "value": str(config.causal_window_minutes)},
        {"name": "HOME", "value": "/scratch"},
        {"name": "OUTPUT_DIR", "value": "/scratch/out"},
    ]
    if config.anthropic_api_key_configured:
        env.append(
            {
                "name": "ANTHROPIC_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "postmortem-secrets",
                        "key": "anthropic-api-key",
                    }
                },
            }
        )
    if config.openrouter_api_key_configured:
        env.append(
            {
                "name": "OPENROUTER_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "postmortem-secrets",
                        "key": "openrouter-api-key",
                    }
                },
            }
        )
    return env


def _owner_reference(created_job: Any) -> dict[str, Any]:
    """Points the ConfigMap at the Job, so `ttlSecondsAfterFinished` reaping
    the Job cascades to the ConfigMap too — one less cleanup mechanism to
    write and get wrong. `created_job` is whatever the injected batch API
    returns; the real `kubernetes` client returns a `V1Job` with `.metadata`,
    a test double can return anything with the same shape."""
    metadata = created_job.metadata
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "name": metadata.name,
        "uid": metadata.uid,
        "controller": True,
        "blockOwnerDeletion": True,
    }


def _object_name(incident_id: str) -> str:
    """`INC-ALERT-<12 hex>` -> `pm-inc-alert-<12 hex>`, safe as both a
    ConfigMap and a Job name (k8s names are lowercase DNS labels)."""
    return f"{_NAME_PREFIX}{incident_id.lower()}"
