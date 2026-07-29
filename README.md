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

**Phase 1 is complete and its gate has passed.** Collectors → graph →
candidate links → ranked hypotheses → drafted postmortem → mechanical
validation, with a bounded repair loop.

| Golden incident | Root cause | Leading confidence | Runner-up | Coverage | Hallucinated |
|---|---|---|---|---|---|
| `inc-0001-missing-env-var` | correct | 1.00 likely | 0.09 tentative | 100% | **0** |
| `inc-0004-two-deploys` | correct | 1.00 likely | 0.37 tentative | 100% | **0** |

Both cases are seeded with a plausible decoy; neither decoy appears in the
leading hypothesis. 0004 is the hard one — both changes touch the *same*
service, so only the module the failure actually names separates them.

Two cases is a gate, not a measurement. The eval harness that turns this into
a corpus with numbers is Phase 3; k3s is Phase 2. See [TODO.md](TODO.md).

## What "traceable" actually means

![A generated postmortem beside the graph it was built from](docs/images/postmortem-graph.png)

Left: the generated postmortem. Right: the graph, queried live.

Follow one ID. `52fd7fa8ac6b0fcb` appears as a `[src:...]` tag in the Impact
paragraph, as a row in the code-rendered Timeline, and as a node whose stored
properties include the raw log line it came from — `count: 776`, because
signature collapsing folded 776 log lines into one failure mode.

Nothing in the left pane shipped without the right pane confirming it. The
validator resolves every tag against Neo4j and rejects the document if a single
one fails to resolve, if a cited timestamp disagrees with the node's, or if
coverage falls below 95%.

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

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env                 # add an API key, or set LLM_PROVIDER=mock
docker compose up -d                 # neo4j + valkey, ~20s to healthy
python -m agent.cli schema-init

# `repo/` is generated, not committed — a nested .git would become a gitlink
python evals/incidents/inc-0001-missing-env-var/build_repo.py

python -m agent.cli run --incident evals/incidents/inc-0001-missing-env-var/
# -> out/postmortem_INC-0001.md
# -> out/run_report_INC-0001.json
```

A run exits **non-zero if the document failed validation**, and writes the
rejected draft and the report anyway — you cannot see what was wrong with a
document you were not given.

```bash
python -m agent.cli validate --incident-id INC-0001 \
    --document out/postmortem_INC-0001.md

pytest tests/unit -q          # no containers, no API key
pytest tests/integration -q   # real Neo4j via testcontainers, mock provider
```

Then open the Neo4j browser at http://localhost:7474 and run the timeline query
from [docs/02](docs/02-knowledge-graph.md). **That side-by-side — the document
and the graph it was built from — is the demo.**

## Why k3s and not Kubernetes

Same API, ~10× less overhead, single binary, runs the whole stack on a 4 GB
VM. Everything written here is standard Kubernetes YAML — it would apply
unchanged to EKS/GKE. See [ADR-001](docs/adr/ADR-001-k3s-over-k8s.md).
