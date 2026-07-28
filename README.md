# Postmortem Autopilot

**Evidence-grounded incident postmortems.** A pipeline that reconstructs an
incident timeline from logs, alerts, deploys and commits, then writes a
postmortem in which *every factual claim is mechanically traceable to a
timestamped source event* — or it doesn't ship.

The differentiator is not "an LLM writes a postmortem." It's that the system
**cannot** state a fact it can't cite, because citation validity is checked by
deterministic code against a graph, not by a prompt.

---

## The 60-second version

```
incident window ──► collect evidence ──► temporal knowledge graph
                                              │
                          deterministic candidate causal links
                                              │
                             LLM ranks competing hypotheses
                                              │
                              LLM writes the postmortem draft
                                              │
                     deterministic validator: every claim cited?
                       every cited node ID real? coverage ≥ 95%?
                                    │              │
                                  PASS           FAIL ──► retry with
                                    │                     validator feedback
                                    ▼
                        postmortem_INC-0007.md + graph
```

## Status

Pre-implementation. Design is complete; see [TODO.md](TODO.md) for the phased
build plan. Phase 1 (local, Docker Compose) must work end-to-end before any
k3s work starts.

## Docs

| Doc | What's in it |
|---|---|
| [docs/00-overview.md](docs/00-overview.md) | Problem, scope, non-goals, why it's CV-worthy |
| [docs/01-architecture.md](docs/01-architecture.md) | The three planes, components, data flow, contracts |
| [docs/02-knowledge-graph.md](docs/02-knowledge-graph.md) | Neo4j schema, Cypher, the `memory.py` interface |
| [docs/03-confidence-model.md](docs/03-confidence-model.md) | How confidence is actually computed, and its limits |
| [docs/04-tech-stack.md](docs/04-tech-stack.md) | Every technology + why + honest scoping |
| [docs/05-k3s-deployment.md](docs/05-k3s-deployment.md) | k3s topology, Helm, resource budget, triggers |
| [docs/06-local-dev.md](docs/06-local-dev.md) | Docker Compose, env vars, running the demo incident |
| [docs/07-evaluation.md](docs/07-evaluation.md) | Golden incidents, metrics, CI gates |
| [docs/adr/](docs/adr/) | Architecture decision records (the "why not X" answers) |
| [TODO.md](TODO.md) | Phased task list — the build order |

## Quickstart (once Phase 1 exists)

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY
docker compose up -d          # neo4j + valkey
python -m agent.run --incident evals/incidents/inc-0001-missing-env-var/
```

## Why k3s and not Kubernetes

Same API, ~10× less overhead, single binary, runs the whole stack on a 4 GB
VM. Everything written here is standard Kubernetes YAML — it would apply
unchanged to EKS/GKE. See [ADR-001](docs/adr/ADR-001-k3s-over-k8s.md).
