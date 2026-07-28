# 05 — k3s Deployment

## Why k3s

Same Kubernetes API, one ~70 MB binary, ~512 MB control-plane footprint
instead of ~2 GB. Batteries included: Traefik, CoreDNS, metrics-server,
local-path storage, ServiceLB. Everything in this project is standard
Kubernetes YAML, so it lifts to EKS/GKE unchanged — see
[ADR-001](adr/ADR-001-k3s-over-k8s.md).

## Cluster setup

```bash
# Single-node. Disable what the project doesn't use.
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="\
  --disable=servicelb \
  --write-kubeconfig-mode=644 \
  --kube-controller-manager-arg=terminated-pod-gc-threshold=20" sh -

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes
```

Traefik and local-path are kept (used for ingress and PVCs). ServiceLB is
disabled — Traefik's NodePort is enough for a single node. The GC threshold
keeps completed pipeline Jobs from accumulating.

For a throwaway dev cluster, `k3d cluster create postmortem -p "8080:80@loadbalancer"`
runs k3s in Docker with equivalent behaviour.

## Namespace layout

```
postmortem        pipeline Jobs, CronJob, webhook receiver
postmortem-data   neo4j, valkey  (stateful, separate lifecycle)
observability     prometheus, alertmanager, loki, grafana
```

Separating `postmortem-data` means the app namespace can be torn down and
reapplied freely during development without touching the graph.

## Topology

```
                    ┌──────────── Traefik Ingress ────────────┐
                    │  /hooks   → receiver                    │
                    │  /grafana → grafana                     │
                    └───────┬──────────────────────┬──────────┘
                            │                      │
        ┌───────────────────▼──────────┐   ┌───────▼─────────┐
        │  webhook-receiver (FastAPI)  │   │    grafana      │
        │  Deployment, 1 replica       │   └───────┬─────────┘
        │  probes: /healthz /readyz    │           │ queries
        └───────────────┬──────────────┘           │
                        │ creates Job              │
                        │ (RBAC: jobs create only) │
                        ▼                          │
        ┌──────────────────────────────┐   ┌───────▼──────────────────┐
        │  pipeline Job (per incident) │   │ prometheus + alertmanager│
        │  backoffLimit: 2             │◄──┤ (alerts also fire the    │
        │  ttlSecondsAfterFinished:1h  │   │  webhook above)          │
        └───────┬──────────────────────┘   └──────────────────────────┘
                │                                    ▲
                │ read/write                         │ scrape /metrics
                ▼                                    │
        ┌──────────────────────────┐                 │
        │ neo4j (StatefulSet, PVC) │                 │
        │ valkey (Deployment)      │─────────────────┘
        └──────────────────────────┘
```

## Trigger paths

Three ways a run starts, in increasing order of realism:

**1. Manual** — the demo path.

```bash
kubectl create job --from=cronjob/postmortem-manual inc-0007 -n postmortem
```

**2. CronJob** — nightly sweep for any incident closed in the last 24h that
has no postmortem. Catches incidents nobody remembered to write up, which is
the actual organizational failure mode this project addresses.

**3. Alertmanager webhook** — the real one. Alertmanager POSTs to the receiver
on resolve; the receiver derives the window from `startsAt`/`endsAt`, and
creates a Job.

```yaml
# alertmanager config
receivers:
  - name: postmortem-autopilot
    webhook_configs:
      - url: http://webhook-receiver.postmortem.svc.cluster.local:8000/hooks/alertmanager
        send_resolved: true
```

The receiver **only creates Jobs** — it doesn't run the pipeline in-process.
That keeps it tiny, always-responsive to probes, and means a pipeline crash
can't take down the thing that accepts alerts. Its ServiceAccount is bound to
a Role granting exactly `create`/`get`/`list` on `jobs` in `postmortem`, and
nothing else.

Dedup: the receiver keys on `(fingerprint, startsAt)` in Valkey with a 6h TTL,
so an alert that flaps five times produces one Job. Without this, a flapping
alert is an API-bill incident of its own.

## Helm chart

```
k3s/helm/postmortem-autopilot/
├── Chart.yaml
├── values.yaml                  # single-node defaults
├── values-dev.yaml              # tiny limits, debug logs, mock LLM
└── templates/
    ├── namespace.yaml
    ├── neo4j-statefulset.yaml
    ├── neo4j-pvc.yaml
    ├── valkey-deployment.yaml
    ├── receiver-deployment.yaml
    ├── receiver-service.yaml
    ├── receiver-rbac.yaml
    ├── ingress.yaml
    ├── cronjob-nightly.yaml
    ├── cronjob-backup.yaml
    ├── job-template.yaml        # rendered by the receiver
    ├── configmap-prompts.yaml   # prompts as config, not baked into the image
    ├── secret-api-keys.yaml     # from --set / sealed-secrets
    └── servicemonitor.yaml      # or a plain scrape annotation
```

Prompts live in a ConfigMap on purpose: iterating on a prompt shouldn't
require a rebuild-and-push cycle, and prompt changes become visible in
`helm diff`.

Install via k3s's native `HelmChart` CRD — drop a manifest in
`/var/lib/rancher/k3s/server/manifests/` and k3s reconciles it on boot. Nice
demo, and it means the cluster rebuilds itself from a single file.

## Storage

`local-path` PVCs on the node's disk:

| PVC | Size | Notes |
|---|---|---|
| `neo4j-data` | 10 Gi | the graph |
| `neo4j-backups` | 5 Gi | nightly dumps, 7-day rotation |
| `prometheus-data` | 5 Gi | 6h retention, sized for headroom |
| `loki-data` | 5 Gi | 72h retention |

Node-local storage means **the data is pinned to the node.** That is the
correct tradeoff for single-node and the wrong one for anything else — say
so explicitly rather than pretending it scales. Multi-node calls for Longhorn
(still lightweight) or a cloud CSI driver.

Backup is a CronJob running `neo4j-admin database dump`, retaining 7 days.
Restore is documented and **must be tested once** — an untested restore is not
a backup, and "I tested the restore" is a genuinely differentiating thing to
be able to say.

## Self-observability

The pipeline exposes `/metrics` and Grafana renders one dashboard:

| Metric | Type | Why it's on the dashboard |
|---|---|---|
| `pm_runs_total{status}` | counter | success / validation_failed / error |
| `pm_run_duration_seconds` | histogram | end-to-end latency |
| `pm_stage_duration_seconds{stage}` | histogram | which stage is slow |
| `pm_events_collected{source}` | gauge | did a collector silently return nothing? |
| `pm_citation_coverage_ratio` | gauge | **the quality metric** |
| `pm_validation_retries_total` | counter | rising = prompt drift |
| `pm_hallucinated_citations_total` | counter | should stay flat at zero |
| `pm_llm_tokens_total{node,model}` | counter | cost attribution per stage |

Two alerts on the pipeline itself, which is a pleasing loop — the incident
tool has incidents:

```yaml
- alert: PostmortemValidationFailing
  expr: increase(pm_runs_total{status="validation_failed"}[1h]) > 2
  annotations:
    summary: "Writer output failing citation validation repeatedly"

- alert: PostmortemHallucinatedCitations
  expr: increase(pm_hallucinated_citations_total[1h]) > 0
  labels: {severity: critical}
  annotations:
    summary: "Model cited a node ID that does not exist — grounding broken"
```

The second one should never fire. If it does, the core promise of the project
is broken and that's exactly the right severity.

## Security notes

Modest, but stated — these are cheap and interviewers ask:

- `ANTHROPIC_API_KEY` in a Secret, mounted as env, never in the image or chart
  defaults.
- Neo4j credentials in a Secret; auth **not** disabled even single-node.
- Pipeline pods: `runAsNonRoot`, `readOnlyRootFilesystem`, all capabilities
  dropped, `seccompProfile: RuntimeDefault`.
- NetworkPolicy: only the pipeline and receiver may reach `postmortem-data`.
- The receiver's RBAC is the narrow Role described above — it can create Jobs
  and nothing else.
- The webhook endpoint validates a shared-secret header. It's reachable from
  the cluster network and should not be an open Job-creation API.
