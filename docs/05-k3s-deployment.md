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
        http_config:
          bearer_token_file: /etc/alertmanager/secrets/webhook-secret
```

Alertmanager's `webhook_configs.http_config` can send a bearer token or basic
auth, but not an arbitrary custom header — so the receiver accepts either
`Authorization: Bearer <secret>` (what Alertmanager sends) or the
`X-Webhook-Secret` header (direct curl/manual testing).

The receiver **only creates Jobs** — it doesn't run the pipeline in-process.
That keeps it tiny, always-responsive to probes, and means a pipeline crash
can't take down the thing that accepts alerts. Its ServiceAccount is bound to
a Role granting `create`/`get`/`list` on `jobs`, plus `create`/`patch` on
`configmaps` in `postmortem` — the second pair because the receiver writes a
per-incident ConfigMap (`meta.yaml` + `alerts.json`) and owner-references it
to the Job it creates, so `ttlSecondsAfterFinished` garbage-collects both
together. Nothing else.

Dedup: the receiver keys on Alertmanager's own `groupKey` plus the derived
window's start time in Valkey with a 6h TTL, so an alert that flaps five
times produces one Job. Without this, a flapping alert is an API-bill
incident of its own.

## Helm charts

Two charts, not one — a deliberate deviation from "one chart installs the
stack": `postmortem-autopilot` gets `helm upgrade`d constantly while
iterating on prompts/linker/confidence weights, `postmortem-observability`
almost never. Bundling them would make every prompt-edit `helm diff` noisier
with unrelated scrape-config churn — undercutting the reason prompts are a
ConfigMap at all.

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
    ├── cronjob-nightly.yaml     # runs `agent.cli sweep`, see Self-observability
    ├── cronjob-backup.yaml
    ├── job-template.yaml        # NOT rendered by the receiver — see its own docstring
    ├── configmap-prompts.yaml   # prompts as config, not baked into the image
    └── secret-api-keys.yaml     # from --set / sealed-secrets

k3s/helm/postmortem-observability/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── namespace.yaml
    ├── prometheus-configmap.yaml       # scrapes Pushgateway only, honor_labels: true
    ├── prometheus-rules-configmap.yaml # the two self-alerts below
    ├── prometheus-deployment.yaml
    ├── pushgateway-deployment.yaml
    ├── alertmanager-configmap.yaml     # routes to the receiver, bearer_token_file
    ├── alertmanager-secret.yaml
    ├── alertmanager-deployment.yaml
    ├── grafana-datasource-configmap.yaml
    ├── grafana-dashboard-configmap.yaml
    └── grafana-deployment.yaml
```

Prompts live in a ConfigMap on purpose: iterating on a prompt shouldn't
require a rebuild-and-push cycle, and prompt changes become visible in
`helm diff`. No ServiceMonitor/Operator CRD here — plain `scrape_configs`,
matching docs/04's stance against kube-prometheus-stack.

Install via k3s's native `HelmChart` CRD — drop a manifest in
`/var/lib/rancher/k3s/server/manifests/` and k3s reconciles it on boot. Nice
demo, and it means the cluster rebuilds itself from a single file.

## Storage

`local-path` PVCs on the node's disk:

| PVC | Size | Notes |
|---|---|---|
| `neo4j-data` | 10 Gi | the graph |
| `neo4j-backups` | 5 Gi | nightly dumps, 7-day rotation |
| `loki-data` | 5 Gi | 72h retention |

Prometheus deliberately has **no PVC** — `emptyDir`, matching Pushgateway's
own in-memory-only state. Losing 6h of scrape history on a pod restart is a
non-event for a stack watching a demo cluster, unlike the graph.

Node-local storage means **the data is pinned to the node.** That is the
correct tradeoff for single-node and the wrong one for anything else — say
so explicitly rather than pretending it scales. Multi-node calls for Longhorn
(still lightweight) or a cloud CSI driver.

Backup is a CronJob (`cronjob-backup.yaml`, `postmortem-data`, `17 2 * * *`)
running `neo4j-admin database dump` for both `neo4j` and `system`, retaining
7 days. Neo4j Community Edition's dump command only runs against an offline
DBMS (Enterprise-only has online backup), so the Job scales the `neo4j`
StatefulSet to 0, dumps from the now-unheld PVC, and scales back to 1 — real,
bounded downtime during the nightly window. A clean shutdown here depends on
the StatefulSet's `KILL` capability (see its own security-context comment):
without it, `tini` can't forward SIGTERM to the JVM at all, a "graceful"
scale-down silently becomes a SIGKILL once the grace period elapses, and the
dump then refuses to run against the resulting unclean log — found for real
against a live cluster, not assumed.

### Restore

**Tested once for real** (not just documented — an untested restore is not a
backup): a fresh, empty PVC, `neo4j-admin database load` against it from a
dump on the `neo4j-backups` volume, then a throwaway single-pod Neo4j
pointed at the restored volume, queried to confirm the data matches. Against
the live cluster's actual data, not a synthetic fixture — the throwaway
target means this never touches the real demo database.

```bash
# 1. A fresh, empty PVC — never the live neo4j-data volume.
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: restore-test-data, namespace: postmortem-data}
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-path
  resources: {requests: {storage: 1Gi}}
EOF

# 2. Load both databases from a dump directory on neo4j-backups.
#    (See cronjob-backup.yaml for the matching container/capability spec —
#    the load Job needs the same security context the dump Job does.)
neo4j-admin database load --from-path=/backups/<timestamp> neo4j --overwrite-destination=true
neo4j-admin database load --from-path=/backups/<timestamp> system --overwrite-destination=true

# 3. Point a throwaway Neo4j at the restored volume and compare counts
#    against the live graph.
kubectl exec -n postmortem-data <restore-pod> -- cypher-shell -u neo4j -p <password> \
  "MATCH (n) RETURN count(n) AS nodes"
kubectl exec -n postmortem-data <restore-pod> -- cypher-shell -u neo4j -p <password> \
  "MATCH ()-[r]->() RETURN count(r) AS rels"

# 4. Delete the throwaway PVC and pod — this was never meant to persist.
```

The `system` database dump/load carries the original credentials — the
throwaway pod's own `NEO4J_AUTH` gets overwritten by whatever `system` was
loaded, so authenticate with the *original* password, not the one set at
pod creation. Confirmed once for real: 21 nodes, 46 relationships, exact
match between the live graph and the restored copy.

## Self-observability

The pipeline never runs as a long-lived process — every invocation is an
ephemeral Job — so there's nothing for Prometheus to scrape directly. Instead
each run pushes its own snapshot to **Pushgateway**, grouped under a
per-incident `incident_id` label (`agent/observability/metrics.py`'s
`push_metrics`); Prometheus scrapes Pushgateway with `honor_labels: true` so
that grouping key survives. Grafana renders one dashboard from what
Pushgateway currently holds:

| Metric | Type | Why it's on the dashboard |
|---|---|---|
| `pm_runs_total{status}` | counter | `status` is `success` or `failure` |
| `pm_run_duration_seconds` | histogram | end-to-end latency |
| `pm_stage_duration_seconds{stage}` | histogram | which stage is slow — **not yet emitted**, a stated gap |
| `pm_events_collected` | histogram | did a collector silently return nothing? |
| `pm_citation_coverage` | histogram | **the quality metric** |
| `pm_validation_retries` | histogram | rising = prompt drift |
| `pm_hallucinated_citations_total` | counter | should stay flat at zero |
| `pm_llm_tokens_total{node}` | counter | cost attribution per stage |

Two alerts on the pipeline itself, which is a pleasing loop — the incident
tool has incidents. Written as `count()`/`sum()` over the currently-retained
Pushgateway set, not `increase()` over a time window: each pushed sample is a
one-shot snapshot from a process that has already exited, and `increase()`
needs the *same* series to change across the window, which a one-shot push
never does.

```yaml
- alert: PostmortemValidationFailing
  expr: count(pm_runs_total{status="failure"}) > 2
  annotations:
    summary: "Writer output failing citation validation repeatedly"

- alert: PostmortemHallucinatedCitations
  expr: count(pm_hallucinated_citations_total > 0) > 0
  labels: {severity: critical}
  annotations:
    summary: "Model cited a node ID that does not exist — grounding broken"
```

The second one should never fire. If it does, the core promise of the project
is broken and that's exactly the right severity.

The retained Pushgateway set has **no automatic pruning** at this project's
scale — a stated, not hidden, limitation. A single-node demo cluster gets
torn down long before unbounded growth there matters; a real deployment would
need Pushgateway's own introspection API polled by something that deletes
stale groups, which this phase deliberately doesn't build.

`cronjob-nightly.yaml` runs `python -m agent.cli sweep`: it finds incidents
that started a run (wrote events to the graph, via `_build_graph_node`) but
never reached one (crashed, was OOM-killed, or exhausted validation
retries), and resumes from graph state alone — no incident directory, no
re-collection, since every graph write is idempotent `MERGE` (invariant 5).
See `Neo4jMemory.find_incidents_needing_postmortem`'s docstring for exactly
what this does and doesn't catch: an incident whose webhook was missed
outright never gets an `Incident` node in the first place, so sweep can't
find it either.

## Security notes

Modest, but stated — these are cheap and interviewers ask:

- `ANTHROPIC_API_KEY` in a Secret, mounted as env, never in the image or chart
  defaults.
- Neo4j credentials in a Secret; auth **not** disabled even single-node.
- Pipeline pods: `runAsNonRoot`, `readOnlyRootFilesystem`, all capabilities
  dropped, `seccompProfile: RuntimeDefault`. Valkey gets the same full
  profile (checkpoint 4: re-tested, not just assumed by analogy to Neo4j —
  it has no mounted volume at all with persistence disabled, so the
  chown-a-volume problem that forces Neo4j's entrypoint to start as root
  never applies to it). Neo4j itself can't be `runAsNonRoot` or
  `readOnlyRootFilesystem` (its entrypoint's chown-then-drop dance needs to
  start as root and unconditionally chowns its whole install tree on every
  boot — confirmed, not assumed), but runs with a narrow, tested capability
  set (`CHOWN, FOWNER, DAC_OVERRIDE, DAC_READ_SEARCH, SETUID, SETGID, KILL`)
  rather than no restriction at all — see `neo4j-statefulset.yaml`'s own
  comment for what each capability is for and how it was found.
- NetworkPolicy: only the pipeline (Jobs and receiver) may reach
  `postmortem-data`. **Stated limitation, not silently assumed working**:
  tested for real on this project's k3d dev cluster, and traffic from a
  different namespace was *not* blocked — k3d's default Flannel setup
  doesn't enforce `NetworkPolicy` the way bare-metal k3s does with its
  bundled kube-router controller. The policy itself is correct and would
  enforce on a real k3s node or any NetworkPolicy-capable CNI (Calico,
  Cilium); it just couldn't be verified as *active* in this specific dev
  environment.
- The receiver's RBAC is the narrow Role described above — it can create
  Jobs and ConfigMaps and nothing else. The backup CronJob's own
  ServiceAccount is separately scoped, narrowly, to scaling the `neo4j`
  StatefulSet and watching its pods — nothing else, and nothing outside
  `postmortem-data`.
- The webhook endpoint validates a shared-secret header. It's reachable from
  the cluster network and should not be an open Job-creation API.
