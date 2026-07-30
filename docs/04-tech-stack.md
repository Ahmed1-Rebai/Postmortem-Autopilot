# 04 — Tech Stack & Rationale

Selection criterion throughout: **the whole system must run on a single 4 GB
node**, because a portfolio project you can't demo on a laptop is a portfolio
project you never demo.

| Layer | Choice | Why this one |
|---|---|---|
| Language | Python 3.12 | LangGraph is Python-native; the ecosystem for Neo4j/Prometheus/Slack clients is there |
| Agent orchestration | **LangGraph** | Explicit state graph with typed state; fits a fixed DAG with a bounded retry edge. Not chosen for agent autonomy — chosen because the graph is *inspectable* |
| LLM | **Claude (Anthropic API)**, or any OpenAI-compatible gateway | Strong instruction-following for the hard constraint "cite every sentence"; `claude-sonnet-5` for the Writer, `claude-opus-5` optional for the Analyst. Model choice is config — the eval harness compares them. `LLM_PROVIDER=openrouter` runs the pipeline against OpenRouter's free tier for zero-cost development; see the scoping note below |
| Long-term memory | **Neo4j 5 Community** | Native traversal + subgraph matching for causal chains and recurrence. Heaviest component; tuned to ~1 GB (see below) |
| Short-term memory | **Valkey** | Redis-compatible, BSD-licensed fork, ~10 MB idle. Working state + LLM response cache |
| Streaming ingestion | **NATS JetStream** *(Phase 4)* | ~15 MB single binary vs. Kafka's JVM + broker + controller. Same "durable stream with consumer groups" story for a fraction of the footprint — [ADR-004](adr/ADR-004-nats-over-kafka.md) |
| Metrics & alerts | **Prometheus + Alertmanager + Pushgateway** | Source of truth for "what fired and when"; Alertmanager webhook is the production trigger. Pushgateway exists because the pipeline only ever runs as an ephemeral Job — there's nothing long-lived to scrape, so each run pushes its own snapshot instead. Single replica, 6h retention, no HA |
| Logs | **Loki** (single-binary, filesystem) | Log source for the collector; monolithic mode avoids the whole read/write/backend split |
| Dashboards | **Grafana** | One dashboard for the pipeline itself: run count, success rate, citation coverage, tokens, latency |
| Containers | **Docker** + multi-stage builds | Final image is `python:3.12-slim`, no build toolchain, ~180 MB |
| Local dev | **Docker Compose** | Phase 1 needs Neo4j + Valkey and nothing else. No cluster required to develop |
| Orchestration | **k3s** | Full Kubernetes API at ~10× less overhead; bundles Traefik, CoreDNS, local-path storage, ServiceLB — [ADR-001](adr/ADR-001-k3s-over-k8s.md) |
| Packaging | **Helm** | One chart installs the stack. Deployed via k3s's `HelmChart` CRD, which is a genuinely nice k3s-native touch |
| Storage | **local-path-provisioner** (bundled) | Node-local PVCs. Correct for single-node; documented as the thing to swap first for multi-node |
| Ingress | **Traefik** (bundled) | Already running in k3s; no reason to add ingress-nginx |
| CI | **GitHub Actions** | lint → unit tests → integration (Neo4j service container) → **eval suite** → build → push |
| GitOps *(stretch)* | **Flux** | ~3 lightweight controllers vs. Argo CD's UI + API + repo + app controllers. Fits the budget |
| Workflow DAG *(stretch)* | **Argo Workflows** | Only if per-stage retry/observability is wanted; honestly *not* needed at 4 stages — [ADR-005](adr/ADR-005-single-job-over-argo.md) |
| Tracing *(stretch)* | **OpenTelemetry → Grafana Tempo** | Per-node latency and token attribution across the LangGraph run |

## Resource budget (single k3s node)

| Component | Memory request | Limit |
|---|---|---|
| k3s server (control plane + kubelet) | ~512 Mi | — |
| Traefik | 64 Mi | 128 Mi |
| CoreDNS + metrics-server | 96 Mi | 192 Mi |
| Neo4j (heap 512 Mi, pagecache 256 Mi) | 1024 Mi | 1280 Mi |
| Valkey | 32 Mi | 64 Mi |
| Prometheus (6h retention) | 320 Mi | 512 Mi |
| Alertmanager | 32 Mi | 64 Mi |
| Pushgateway | 16 Mi | 32 Mi |
| Loki (single-binary) | 192 Mi | 384 Mi |
| Grafana | 96 Mi | 192 Mi |
| Webhook receiver (FastAPI) | 64 Mi | 128 Mi |
| **Steady-state total** | **~2.4 Gi** | |
| Pipeline Job (transient, per incident) | 384 Mi | 768 Mi |
| **Peak during a run** | **~2.8 Gi** | |

Comfortable on a 4 GB VM, roomy on 8 GB. Neo4j is 40% of the footprint —
[ADR-002](adr/ADR-002-neo4j-over-alternatives.md) covers when to swap it for
embedded Kùzu and what that costs.

## Deliberate omissions

| Not using | Why |
|---|---|
| Kafka | JVM + broker + coordinator for one low-volume topic. NATS JetStream is the same story at 5% of the RAM |
| Redis | Valkey is the drop-in fork with a clean license; identical protocol and client |
| A vector DB | Retrieval here is structural, not semantic. Adding embeddings would add a dependency to solve a problem this system doesn't have |
| Argo CD | Four components and a web UI to sync one chart. Flux does it in a third of the memory |
| kube-prometheus-stack | Bundles operators, CRDs and ~10 exporters. A plain Prometheus + Alertmanager Deployment covers this project's needs at a quarter of the footprint |
| Postgres | Nothing here is relational that isn't better as a graph |
| Celery / task queue | The pipeline is a k3s Job. A queue would be a second scheduler competing with the one already running |

## Honest scoping

Solo-project reality, stated up front so it can be stated out loud in an
interview:

- k3s is a **single-node local cluster** (or one cheap VM), not multi-node HA.
- Neo4j runs as a **single Community instance** with a PVC-backed volume and a
  nightly `neo4j-admin dump` CronJob — no clustering, no PITR.
- NATS JetStream, if built at all, is **one server with file storage**, not a
  three-node cluster.
- Loki and Prometheus run at **short retention** and are single-replica.
- Slack/chat collection is the **most likely thing to be documented but not
  built** — it adds OAuth setup for the weakest evidence signal (weight 0.10).
- The eval corpus is **synthetic + a handful of hand-labelled real incidents**,
  not a large real-world dataset. The metrics are real; the sample is small.
- Development runs against **OpenRouter's free tier**, not the Anthropic API.
  That keeps iteration free, but free-tier models are weaker instruction
  followers than `claude-sonnet-5`, and the constraint they're weakest at is
  exactly this project's hard one: a citation tag on every factual sentence.
  Expect citation coverage and precision@1 to read lower than the same pipeline
  would score on Claude. Because the provider is config, the eval suite can
  report both — and *that comparison is itself a result worth showing*: it
  quantifies how much of the grounding guarantee comes from the validator
  rather than from model quality. The validator's guarantee is unaffected
  either way; a weaker model fails validation more often, it does not fabricate
  citations that pass.

Being able to explain *why* each piece is there and what its tradeoff is
matters more than claiming production scale. The failure mode in interviews is
overclaiming, not underclaiming.
