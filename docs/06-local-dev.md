# 06 — Local Development

Phase 1 needs **no cluster**. Two containers and a Python venv.

## Prerequisites

- Python 3.12+
- Docker + Docker Compose v2
- An Anthropic API key (or run with `LLM_PROVIDER=mock`)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r agent/requirements.txt
cp .env.example .env      # fill in ANTHROPIC_API_KEY
docker compose up -d      # neo4j + valkey only
python -m agent.cli schema-init
```

Neo4j browser: http://localhost:7474 (`neo4j` / whatever's in `.env`).
Being able to *see* the graph after a run is most of the debugging story —
open it early and often.

## `docker-compose.yml`

Deliberately minimal. Prometheus, Loki, Grafana and NATS are **not** here —
Phase 1 collectors read fixtures from disk, so running an observability stack
locally would be overhead with no payoff. They arrive in Phase 2 as k3s
workloads.

```yaml
services:
  neo4j:
    image: neo4j:5-community
    ports: ["7474:7474", "7687:7687"]
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD}
      NEO4J_server_memory_heap_max__size: 512m
      NEO4J_server_memory_pagecache_size: 256m
    volumes: ["neo4j_data:/data"]
    healthcheck:
      test: ["CMD", "cypher-shell", "-u", "neo4j", "-p", "${NEO4J_PASSWORD}", "RETURN 1"]
      interval: 10s
      retries: 10

  valkey:
    image: valkey/valkey:8-alpine
    ports: ["6379:6379"]
    command: ["valkey-server", "--save", "", "--appendonly", "no"]

volumes:
  neo4j_data:
```

Valkey runs with persistence off — it's a cache, and restarting it must be a
non-event. If losing Valkey ever breaks a run, that's a bug in the pipeline,
not a reason to enable AOF.

## `.env.example`

```bash
# --- LLM ---
ANTHROPIC_API_KEY=sk-ant-...
LLM_PROVIDER=anthropic              # anthropic | mock
ANALYST_MODEL=claude-sonnet-5
WRITER_MODEL=claude-sonnet-5

# --- Neo4j ---
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=changeme-local-only

# --- Valkey ---
VALKEY_URL=redis://localhost:6379/0

# --- Pipeline tuning ---
CAUSAL_WINDOW_MINUTES=15
CITATION_COVERAGE_THRESHOLD=0.95
MAX_VALIDATION_RETRIES=2
MAX_EVENTS_PER_RUN=5000

# --- Collector sources (Phase 1: local fixtures) ---
LOG_SOURCE_PATH=./evals/fixtures/logs
GIT_REPO_PATH=./evals/fixtures/repo
ALERTS_FIXTURE=./evals/fixtures/alerts.json
```

`LLM_PROVIDER=mock` returns canned Analyst/Writer responses. It exists so the
whole pipeline — including the validator's retry loop — can be exercised in CI
and in tests without spending tokens or depending on the network.

## Repo layout

```
new_project/
├── agent/
│   ├── cli.py                  # entrypoint: run, schema-init, validate, eval
│   ├── graph.py                # LangGraph DAG assembly
│   ├── state.py                # PipelineState + dataclasses
│   ├── memory.py               # the ONLY file with Cypher in it
│   ├── config.py               # env + YAML config loading
│   ├── collectors/
│   │   ├── base.py             # Collector protocol
│   │   ├── logs.py  alerts.py  git.py  deploys.py  chat.py
│   ├── normalize/
│   │   ├── events.py           # RawRecord -> Event
│   │   ├── signatures.py       # error fingerprinting
│   │   └── services.py         # service alias canonicalization
│   ├── linker.py               # candidate causal edges (the 3 heuristics)
│   ├── confidence.py           # the scoring model — pure functions
│   ├── nodes/
│   │   ├── analyst.py  writer.py  validator.py  recurrence.py
│   ├── prompts/
│   │   ├── analyst.md  writer.md  repair.md
│   ├── render/
│   │   ├── timeline.py         # code-rendered timeline (no LLM)
│   │   └── document.py         # assembles final markdown
│   └── observability/
│       ├── metrics.py          # prometheus_client
│       └── report.py           # RunReport
├── evals/
│   ├── incidents/              # golden incidents + expected root cause
│   ├── fixtures/               # logs, repo, alerts
│   ├── runner.py
│   └── metrics.py
├── tests/
│   ├── unit/                   # no network, no containers
│   └── integration/            # real Neo4j via testcontainers
├── k3s/
│   ├── helm/postmortem-autopilot/
│   └── manifests/              # k3s HelmChart CRD bootstrap
├── docs/
├── docker-compose.yml
├── Dockerfile
├── TODO.md
├── CLAUDE.md
└── README.md
```

## Running the demo

```bash
python -m agent.cli run --incident evals/incidents/inc-0001-missing-env-var/
# -> out/postmortem_INC-0001.md
# -> out/run_report_INC-0001.json
```

Then open Neo4j browser and run the timeline query from
[02-knowledge-graph.md](02-knowledge-graph.md) to see the graph the document
was built from. **That side-by-side — document and graph — is the demo.** It's
what makes "every claim is traceable" a thing someone can watch rather than a
claim on a slide.

## Testing

```bash
pytest tests/unit -q                       # fast, no containers, no API key
pytest tests/integration -q                # spins up Neo4j via testcontainers
python -m evals.runner --all               # the eval suite (needs API key)
python -m evals.runner --all --mock        # structural checks only, free
```

Test priorities, in order:

1. **`normalize/signatures.py`** — pure function, high bug density, cheap to
   test exhaustively. Wrong fingerprinting silently ruins every downstream
   number.
2. **`confidence.py`** — pure functions with a table of expected scores. This
   is the number the whole product hangs on.
3. **`linker.py`** — given a fixture event list, assert exactly which
   candidate edges appear. Off-by-one window bugs live here.
4. **`nodes/validator.py`** — feed it hand-written good and bad documents
   (missing citation, fabricated ID, wrong timestamp, citation from another
   incident) and assert each is caught. This is the safety net; test it like
   one.
5. **`memory.py`** — integration tests against real Neo4j. Mocking a graph
   driver tests the mock.

The Analyst and Writer are exercised through the eval suite rather than unit
tests — asserting on LLM prose is brittle, asserting on eval metrics isn't.

## Common problems

| Symptom | Cause |
|---|---|
| Neo4j refuses connections for ~30s after `up` | it's still starting; the compose healthcheck covers it, wait for healthy |
| Duplicate events after a rerun | ID isn't content-derived, or `MERGE` was written as `CREATE` |
| Every hypothesis scores identically | the linker is emitting only `temporal_proximity`; check service canonicalization |
| Validation fails on every run | coverage threshold vs. an over-eager "factual sentence" classifier — check `validation.json`, not the prompt, first |
| Token cost per run is surprising | the graph serialization sent to the Analyst is unbounded; signature collapsing should cap it |
