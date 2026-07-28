# TODO — Build Order

**Rule: phases are sequential.** Do not start Phase 2 until Phase 1 produces a
correct, validated postmortem for at least two golden incidents. The most
common way this project fails is building k3s manifests for a pipeline that
doesn't work yet.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · 🔴 blocking · ⭐ the
differentiator work

---

## Phase 0 — Scaffolding

- [x] `git init`, `.gitignore` (`.env`, `out/`, `.venv/`, `evals/results/`)
- [x] Repo skeleton per [docs/06-local-dev.md](docs/06-local-dev.md)
- [x] `agent/requirements.txt` — langgraph, langchain-core, anthropic, neo4j,
      redis, pydantic, python-dotenv, PyGithub, prometheus-client, pyyaml,
      pytest, testcontainers
      *(runtime deps here; pytest/testcontainers/ruff/mypy in
      `requirements-dev.txt` so the Phase 2 image stays ~180 MB)*
- [x] `docker-compose.yml` (neo4j + valkey only)
- [x] `.env.example`
- [x] `agent/config.py` — env + `config/confidence.yaml` loading
- [x] `pyproject.toml` — ruff + mypy, both wired into CI from day one
      *(`.github/workflows/ci.yml`: ruff → mypy → `pytest tests/unit`)*

## Phase 1 — MVP (local, Docker Compose) 🔴

The whole point of this phase: **one incident in, one validated postmortem
out.** Nothing else matters until that works.

### 1.1 Data model & memory
- [x] `agent/state.py` — `Event`, `Incident`, `Hypothesis`, `CandidateLink`,
      `ValidationReport`, `RunReport`, `PipelineState`
      *(plus `Window`, `Chain`, `CitationCheck`, `SimilarIncident`,
      `Complaint` — the supporting types the listed ones need to be
      well-formed)*
- [x] `agent/memory.py` — full interface from
      [docs/02-knowledge-graph.md](docs/02-knowledge-graph.md)
      *(a `Neo4jMemory` class rather than module-level functions, so the
      driver is injected instead of global — see the module docstring)*
- [x] `ensure_schema()` — constraints + indexes, idempotent
- [x] Integration test: write 50 events twice → assert exactly 50 nodes
      (idempotency is load-bearing; a duplicate silently inflates every
      confidence score)
      *(mutation-checked: `MERGE`→`CREATE` fails the test)*

### 1.2 Collection & normalization
- [x] `collectors/base.py` — the `Collector` protocol
- [x] `collectors/logs.py` — plain + JSON-lines log files
- [x] `collectors/git.py` — local repo via `git log`, capture `files_changed`
- [x] `collectors/alerts.py` — Alertmanager-shaped JSON fixture
- [x] `normalize/signatures.py` ⭐ — strip UUIDs/hex/numbers/quoted
      strings/paths → error fingerprint
- [x] `normalize/services.py` — canonical names + alias map
      *(`config/services.yaml`, loaded via `config.py`)*
- [x] `normalize/events.py` — `RawRecord` → `Event`, content-derived IDs
      *(also collapses log errors by `(service, signature)`; measured 150×
      on a 3k-line fixture)*
- [x] Unit tests for signatures: table of ~30 real-looking log lines →
      expected fingerprints. Do this thoroughly; everything downstream
      depends on it
      *(30-row table + collapse groups + discrimination pairs, 52 tests)*

### 1.3 Graph construction
- [x] `GraphWriter` — `MERGE` events, `:PART_OF`, `:AFFECTS`
      *(`memory.write_events`; invariant 6 keeps the Cypher there rather than
      in a separate writer class)*
- [x] `link_temporal()` — `PRECEDES` chain (consecutive only, not transitive
      closure — that explodes)
      *(`memory.link_temporal`, done in 1.1)*
- [x] `agent/linker.py` ⭐ — the three heuristics:
  - [x] temporal proximity (configurable window)
  - [x] service overlap (uses canonicalized names)
  - [x] change-path overlap (commit files → service/module map)
        *(matches a path segment against the failing service, **or** a
        distinctive file stem against the failure's signature — the
        `app/pool.py` ↔ "pool exhausted" case)*
- [x] Record *which* heuristics fired on each `POSSIBLY_CAUSED` edge
- [x] Unit test: fixture event list → assert the exact candidate edge set

### 1.4 Confidence
- [x] `agent/confidence.py` ⭐ — the model from
      [docs/03-confidence-model.md](docs/03-confidence-model.md), pure functions
- [x] Availability-aware denominator (unavailable source ⇒ excluded, not
      penalized)
- [x] Contradiction penalty, capped at 0.4
- [x] Bands: likely / plausible / tentative
- [x] `config/confidence.yaml` — weights as config *(landed in Phase 0)*
- [x] Unit tests: table of scenarios → expected scores, including both worked
      examples from the doc *(53 tests; H1 → 1.00 likely, H2 → 0.29 tentative)*

> **Open question for 1.5+:** on the demo incident the decoy chain scores 0.72
> against the real cause's 0.78, because `change_path_overlap` fires whenever a
> commit's path segment matches the failing service — which is nearly every
> commit in a repo laid out by service. The strong half of that signal (file
> stem ↔ failure text) carries no extra weight. Splitting it would change the
> signal set in [docs/03](docs/03-confidence-model.md), so it needs an ADR.

### 1.5 Reasoning plane
- [ ] `nodes/analyst.py` — rank hypotheses, ≥2 when ≥2 chains exist
- [ ] Disconfirming evidence is a **required** output field
- [ ] `prompts/analyst.md` — structured JSON output, node IDs only
- [ ] `render/timeline.py` — timeline rendered **by code**, not the LLM
- [ ] `nodes/writer.py` — Summary, Impact, Hypotheses, Contributing Factors,
      Corrective Actions, Open Questions
- [ ] `prompts/writer.md` — `[src:<id>, <ts>]` on every factual sentence
- [ ] `render/document.py` — assemble code-rendered + LLM-written sections
- [ ] Mock LLM provider (`LLM_PROVIDER=mock`) — must land here, not later; CI
      and the retry-loop tests depend on it

### 1.6 Verification plane ⭐ 🔴
This is the project. Give it real attention.
- [ ] `nodes/validator.py`:
  - [ ] extract all `[src:...]` tags
  - [ ] one `resolve_citations()` round trip
  - [ ] uncited factual sentences → complaint
  - [ ] unresolvable ID → **hallucination**, hard failure
  - [ ] cited timestamp ≠ node timestamp → complaint
  - [ ] coverage ratio vs. threshold
- [ ] "Factual sentence" classifier — deliberately dumb regex/heuristics,
      section allow-list; its accuracy is itself measured in evals
- [ ] Structured `ValidationReport`, not a boolean
- [ ] LangGraph retry edge: validate → write, max 2, feedback injected
- [ ] Fail loudly after 2 retries: write draft + report to disk, exit non-zero
- [ ] Unit tests with hand-written bad documents: missing citation, fabricated
      ID, wrong timestamp, ID from another incident

### 1.7 Wiring
- [ ] `agent/graph.py` — LangGraph DAG
- [ ] `agent/cli.py` — `run`, `schema-init`, `validate`, `eval`
- [ ] `observability/report.py` — `RunReport` JSON per run
- [ ] `observability/metrics.py` — prometheus_client counters/histograms

### 1.8 First golden incidents
- [ ] `inc-0001-missing-env-var` — full fixtures + `expected.yaml`
- [ ] `inc-0004-two-deploys` — with a decoy (the discrimination case)
- [ ] 🔴 **Gate: both produce validated postmortems with the correct root
      cause.** Do not proceed past this line.
- [ ] Screenshot: postmortem + Neo4j graph side by side → README

## Phase 2 — k3s

- [ ] `Dockerfile` — multi-stage, `python:3.12-slim`, non-root, ~180 MB
- [ ] Install k3s per [docs/05-k3s-deployment.md](docs/05-k3s-deployment.md)
- [ ] Helm chart skeleton + `values.yaml` / `values-dev.yaml`
- [ ] Neo4j StatefulSet + PVC (tuned heap/pagecache)
- [ ] Valkey Deployment
- [ ] Pipeline `Job` template (`backoffLimit: 2`, `ttlSecondsAfterFinished`)
- [ ] `kubectl create job --from=cronjob/...` runs end-to-end in-cluster
- [ ] Webhook receiver (FastAPI): `/hooks/alertmanager`, `/healthz`, `/readyz`
- [ ] Receiver creates Jobs; narrow RBAC (`jobs` create/get/list only)
- [ ] Dedup on `(fingerprint, startsAt)` in Valkey, 6h TTL
- [ ] Shared-secret header on the webhook
- [ ] Traefik Ingress
- [ ] Nightly sweep CronJob (incidents closed <24h with no postmortem)
- [ ] Prometheus + Alertmanager + Grafana in `observability`
- [ ] Alertmanager → receiver wired, tested with a real firing alert
- [ ] Grafana dashboard: the 8 metrics from the deployment doc
- [ ] The two self-alerts (`ValidationFailing`, `HallucinatedCitations`)
- [ ] Neo4j backup CronJob (`neo4j-admin dump`, 7-day rotation)
- [ ] 🔴 **Test the restore once.** An untested restore is not a backup
- [ ] Pod security: non-root, read-only rootfs, caps dropped, seccomp
- [ ] NetworkPolicy for `postmortem-data`
- [ ] k3s `HelmChart` CRD bootstrap manifest

## Phase 3 — Recurrence & Evaluation ⭐

The phase that makes this more than a demo.

### 3.1 Recurrence memory
- [ ] `compute_fingerprint()` — `(cause_type, effect_type, service, signature)`
- [ ] `find_similar_incidents()` — the Cypher in the graph doc
- [ ] `CorrectiveAction` nodes + `REMEDIATED_BY`
- [ ] `nodes/recurrence.py` — inject `## Similar Past Incidents`
- [ ] Surface **open** corrective actions from past matching incidents — this
      is the money feature ("third time; the March fix is still open")
- [ ] `SIMILAR_TO` edges persisted post-run
- [ ] Golden incident #07 (recurrence of #01) passes

### 3.2 Eval harness
- [ ] `evals/runner.py` + `evals/metrics.py`
- [ ] Remaining golden incidents 02, 03, 05, 06, 08, 09, 10
- [ ] Every case seeded with a plausible decoy
- [ ] All 10 metrics from [docs/07-evaluation.md](docs/07-evaluation.md)
- [ ] Result JSON + summary table + markdown report
- [ ] `--sweep-weights` for confidence sensitivity
- [ ] `--mock` for free structural runs
- [ ] `evals/results/history.jsonl` for trend plotting

### 3.3 CI
- [ ] GitHub Actions: ruff → mypy → unit → integration (Neo4j service) → build
- [ ] `evals-quick` (mock) on every PR
- [ ] Full eval nightly + on `main`
- [ ] Hard gates: hallucinated citations `== 0`, coverage `≥ 0.95`,
      precision@1 `≥ 0.70`
- [ ] Push image to GHCR
- [ ] Eval metrics table auto-updated in the README

## Phase 4 — Optional stretch

Only after Phases 1–3 are genuinely done. Each is independent; pick by
interest, not by CV keyword count.

- [ ] Live collectors: Loki API, Prometheus API, GitHub API
- [ ] NATS JetStream rolling evidence buffer ([ADR-004](docs/adr/ADR-004-nats-over-kafka.md))
- [ ] OpenTelemetry tracing → Tempo, per-node latency and token attribution
- [ ] Flux GitOps
- [ ] Slack collector (weakest signal — build last or never)
- [ ] Retention CronJob: downsample incidents older than the last 200
- [ ] Model comparison in the eval report (sonnet vs. opus, cost vs. accuracy)
- [ ] Argo Workflows — **read [ADR-005](docs/adr/ADR-005-single-job-over-argo.md)
      first;** the current answer is "don't"

## Portfolio checklist

- [ ] README: architecture diagram (the three planes), eval metrics table,
      demo GIF of a run
- [ ] The side-by-side screenshot: postmortem citations ↔ Neo4j nodes
- [ ] Public repo, clean history, real commit messages
- [ ] 2–3 CV bullets, past tense, quantified from **real eval numbers** —
      never invented ones
- [ ] A written "what I'd do differently at 10× scale" section
- [ ] Be ready to say which parts are POC vs. production-grade
      ([04-tech-stack.md](docs/04-tech-stack.md) scoping note)
- [ ] Be ready to explain the confidence model's limits before being asked
      ([03-confidence-model.md](docs/03-confidence-model.md))

---

## Traps

Recorded because they are the specific ways this project goes wrong:

1. **Building k3s manifests before the pipeline works.** Infrastructure is the
   fun part and the wrong part to do first.
2. **Skipping the mock LLM provider.** Without it, tests need an API key,
   so they don't get run, so the retry loop stays untested.
3. **Non-idempotent event IDs.** Duplicates inflate corroboration counts and
   silently corrupt every confidence score. Very hard to notice later.
4. **A weak validator.** If it doesn't actually resolve IDs against Neo4j, the
   project's central claim is false and nothing else compensates.
5. **Golden incidents without decoys.** Measures nothing.
6. **Unbounded graph serialization to the Analyst.** Signature collapsing must
   cap it, or token cost scales with log volume.
7. **Chasing Phase 4 keywords.** A finished Phase 1–3 beats a half-built
   Phase 4 in every interview.
