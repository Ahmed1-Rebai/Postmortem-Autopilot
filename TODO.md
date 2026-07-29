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

> **Resolved in 1.8, without changing the signal set.** `change_path_overlap`
> was firing on a bare path-segment match, so in a repo laid out by service it
> fired for every in-window commit and the 0.30-weight signal stopped
> discriminating. The path branch is now suppressed when `service_overlap` has
> already established the same fact — de-duplication, not a weakening, since a
> commit spanning two services still has no service and the branch still
> applies. Confidence separation went from 0.06 → 0.34 on 0001 and 0.00 → 0.34
> on 0004, against the ≥0.20 target in
> [docs/07](docs/07-evaluation.md).

### 1.5 Reasoning plane
- [x] `nodes/analyst.py` — rank hypotheses, ≥2 when ≥2 chains exist
      *(also re-ranks by computed confidence: the model proposes an order,
      the number decides it)*
- [x] Disconfirming evidence is a **required** output field
- [x] `prompts/analyst.md` — structured JSON output, node IDs only
- [x] `render/timeline.py` — timeline rendered **by code**, not the LLM
- [x] `nodes/writer.py` — Summary, Impact, Hypotheses, Contributing Factors,
      Corrective Actions, Open Questions
- [x] `prompts/writer.md` — `[src:<id>]` on every factual sentence
- [x] `render/document.py` — assemble code-rendered + LLM-written sections
- [x] Mock LLM provider (`LLM_PROVIDER=mock`) — must land here, not later; CI
      and the retry-loop tests depend on it
- [x] `agent/llm.py` — provider protocol; mock / anthropic / openrouter
      *(not in the original list, but the nodes need something to call)*

> **Finding from the first real run (2026-07-28):** the model reads
> `contradicting_event_ids` as "everything unrelated", listing the decoy commit
> and other services' events as disconfirming. Three "contradictions" cost the
> full 0.40 penalty, so the *correct* root cause landed at 0.60 plausible
> instead of likely. Unrelated ≠ contradicting. Fix is a sharper prompt
> definition, and possibly a code filter requiring a contradicting event to
> share the hypothesis's service or signature.

### 1.6 Verification plane ⭐ 🔴
This is the project. Give it real attention.
- [x] `nodes/validator.py`:
  - [x] extract all `[src:...]` tags
  - [x] one `resolve_citations()` round trip *(asserted by test)*
  - [x] uncited factual sentences → complaint
  - [x] unresolvable ID → **hallucination**, hard failure
  - [x] cited timestamp ≠ node timestamp → complaint *(and fails the document —
        a citation naming the right event at the wrong time reads as checked)*
  - [x] coverage ratio vs. threshold
- [x] "Factual sentence" classifier — deliberately dumb regex/heuristics,
      section allow-list; its accuracy is itself measured in evals
- [x] Structured `ValidationReport`, not a boolean
- [x] Retry loop: validate → write, max 2, feedback injected
      *(`revise_until_valid`, with the rewriter injected so it is testable
      without a model; Phase 1.7 expresses it as the LangGraph edge)*
- [x] Fail loudly after 2 retries: write draft + report to disk, exit non-zero
      *(`cli.cmd_run`, exit code 1; artifacts written pass or fail)*
- [x] Unit tests with hand-written bad documents: missing citation, fabricated
      ID, wrong timestamp, ID from another incident

> **Verified against the real model (2026-07-28):** first draft 57.1% coverage
> → rejected with 10 complaints → one rewrite → 100% coverage, 0 hallucinated,
> passed. 114s, two model calls.

### 1.7 Wiring
- [x] `agent/graph.py` — LangGraph DAG *(one bounded retry edge; the branch is
      the only conditional in the pipeline)*
- [x] `agent/cli.py` — `run`, `schema-init`, `validate`, `eval`
      *(`eval` reports that the harness lands in Phase 3.2 rather than
      pretending; a failing run exits 1 and still writes its artifacts)*
- [x] `observability/report.py` — `RunReport` JSON per run
- [x] `observability/metrics.py` — prometheus_client counters/histograms

> **First full CLI run (2026-07-28):** `INC-0001: PASS · 6 events ·
> 14 candidates · 3 hypotheses · coverage 100.0% · hallucinated 0 · retries 2 ·
> 185.6s · 18,434 tokens`. It used the entire retry budget — see the note under
> 1.8 before treating that as comfortable.

### 1.8 First golden incidents
- [x] `inc-0001-missing-env-var` — full fixtures + `expected.yaml`
      *(built during 1.7 so the CLI had something real to run; `repo/` is
      generated by `build_repo.py`, not committed)*
- [x] `inc-0004-two-deploys` — with a decoy (the discrimination case)
      *(both changes touch the **same service**, so temporal proximity and
      service overlap favour the decoy — only the module the failure names
      separates them)*
- [x] 🔴 **Gate: both produce validated postmortems with the correct root
      cause.** Do not proceed past this line.

> **Gate result (2026-07-29):**
>
> | | INC-0001 | INC-0004 |
> |---|---|---|
> | verdict | PASS | PASS |
> | root cause | correct | correct |
> | leading confidence | 1.00 likely | 1.00 likely |
> | runner-up | 0.09 tentative | 0.37 tentative |
> | citation coverage | 100% | 100% |
> | hallucinated citations | 0 | 0 |
> | decoy terms in top hypothesis | none | none |
> | retries | 1 | 2 |
>
> Building 0004 broke the gate first, and that was the point: the decoy and
> the real cause both scored 0.78, a confidence separation of **exactly zero**
> on the case built to measure discrimination. See the linker fix below.

- [x] Screenshot: postmortem + Neo4j graph side by side → README
      *(`docs/images/postmortem-graph.png`; follow `52fd7fa8ac6b0fcb` across
      the citation, the timeline row, and the node's raw_json)*

## Phase 2 — k3s

Split into checkpoints, same discipline as Phase 1's 1.1–1.8: build, verify
for real against a live cluster, commit, report, continue. Checkpoint 1
(below) is the load-bearing claim — "this runs in k8s, not just
docker-compose" — everything else is follow-on.

### Checkpoint 1 — core loop 🔴
- [x] `Dockerfile` — multi-stage, non-root, ~180 MB
      *(143 MB, under target. **Alpine, not slim-Debian** — measured, not
      assumed: Debian's `git` package pulled perl+libcurl+gnutls+krb5 as hard
      Depends, 99 MB on its own, more than half the image. Alpine's git has
      no such chain, and every dependency in `agent/requirements.txt` ships
      musllinux wheels — confirmed via the pip install log, not assumed)*
- [x] Install k3s per [docs/05-k3s-deployment.md](docs/05-k3s-deployment.md)
      *(**k3d, not bare k3s** — `sudo` needs an interactive password this
      session can't supply, so the `curl\|sh` root installer isn't viable.
      docs/05 names this exact fallback itself. `k3d`/`helm` installed to
      `~/.local/bin`, no root anywhere)*
- [x] Helm chart skeleton + `values.yaml` / `values-dev.yaml`
- [x] Neo4j StatefulSet + PVC (tuned heap/pagecache)
- [x] Valkey Deployment
- [x] Pipeline `Job` template (`backoffLimit: 2`, `ttlSecondsAfterFinished`)
      *(a suspended CronJob, `postmortem-manual` — `kubectl create job
      --from=cronjob/...` is k8s's own mechanism for stamping a Job from it)*
- [x] `kubectl create job --from=cronjob/...` runs end-to-end in-cluster

> **Checkpoint 1 result (2026-07-29):** `kubectl logs` on the real Job:
> `INC-0001: PASS · 6 events · 14 candidates · 2 hypotheses · coverage
> 100.0% · hallucinated 0 · retries 0 · 24.8s · 1836 tokens` — the identical
> summary format proven locally and in plain Docker, now reproduced from a
> real Kubernetes Job, non-root uid 10001, `readOnlyRootFilesystem: true`,
> all capabilities dropped, in-cluster Neo4j reached over Service DNS, `git`
> evidence from the baked-in fixture repo, prompts served live from a
> `--set-file`-populated ConfigMap (confirmed via the Job's own rendered
> spec: `PROMPTS_DIR=/config/prompts`, mounted).
>
> **Two real capability findings, not guessed at:** Neo4j's and Valkey's
> official images both start as uid 0 with no documented non-root mode
> (confirmed by running each image directly and checking `id`) — enforcing
> `runAsNonRoot` from outside makes k8s refuse to start them at all. For
> Neo4j specifically, `capabilities: {drop: [ALL], add: [CHOWN, FOWNER]}`
> gets past its chown-the-volume step but then fails traversing the same
> tree (`find: /var/lib/neo4j: Permission denied` — CAP_DAC_OVERRIDE
> territory: "root" without it still respects ordinary permission bits).
> Chasing the exact minimal capability set for an undocumented upstream
> entrypoint is Checkpoint 4 hardening work, not this one's — the pipeline
> container itself (fully under our control) runs the complete hardened
> profile; Neo4j and Valkey get a stated, investigated exception, not a
> silent one. `config/prompts` are also not baked as a second copy: `helm`'s
> `--set-file` reads `agent/prompts/*.md` directly at install time, so
> there's exactly one copy of each prompt on disk, and the ConfigMap is
> genuinely live-editable via `helm upgrade` with no rebuild.

### Checkpoint 2 — webhook receiver
- [ ] Webhook receiver (FastAPI): `/hooks/alertmanager`, `/healthz`, `/readyz`
      *(new top-level `receiver/` package, its own Dockerfile — kept out of
      the pipeline image, different failure-isolation story)*
- [ ] Receiver creates Jobs; narrow RBAC (`jobs` create/get/list only)
      *(plus `configmaps: create, patch` — the receiver synthesizes each
      incident's `meta.yaml`/`alerts.json` as a ConfigMap owner-referenced to
      its Job, so `ttlSecondsAfterFinished` garbage-collects both together)*
- [ ] Dedup on `(fingerprint, startsAt)` in Valkey, 6h TTL
      *(Alertmanager's own per-alert `fingerprint`, not `Incident.fingerprint`
      in `agent/state.py` — same word, unrelated concepts, different layers)*
- [ ] Shared-secret header on the webhook
- [ ] Traefik Ingress

### Checkpoint 3 — observability
- [ ] Nightly sweep CronJob (incidents closed <24h with no postmortem)
      *(needs real code first: a `sweep` subcommand +
      `memory.find_incidents_needing_postmortem()` — otherwise this is a
      schedule with nothing correct to run, TODO's own trap #1)*
- [ ] Prometheus + Alertmanager + Grafana in `observability`
      *(a second, small chart — deviates from docs/04's "one chart" line,
      because the pipeline chart gets `helm upgrade`d constantly while
      iterating on prompts/weights and this one almost never; bundling them
      makes every prompt-edit `helm diff` noisier with unrelated scrape-config
      churn, undercutting the reason prompts are a ConfigMap at all)*
- [ ] Alertmanager → receiver wired, tested with a real firing alert
- [ ] Grafana dashboard: the 8 metrics from the deployment doc
- [ ] The two self-alerts (`ValidationFailing`, `HallucinatedCitations`)
      *(needs a genuine fix, not just infra: per-Job textfiles/naive
      Pushgateway pushes don't give `increase()` semantics for single-shot
      ephemeral processes — each Job's counters start at 0. Add Pushgateway,
      push per-incident-labeled snapshots, rewrite both alerts from
      `increase(...)` to `count()`/`sum()` over the nightly-swept retained set)*
- [ ] ~~Loki~~ — cut from Phase 2 entirely; nothing in Phases 1–3 produces log
      volume for it to ingest. Deferred to Phase 4 with live collectors.

### Checkpoint 4 — hardening & backup
- [ ] Neo4j backup CronJob (`neo4j-admin dump`, 7-day rotation)
- [ ] 🔴 **Test the restore once.** An untested restore is not a backup
- [ ] Pod security: non-root, read-only rootfs, caps dropped, seccomp
      *(the pipeline Job already has this — this item is finishing the job
      for Neo4j/Valkey, see checkpoint 1's capability finding above)*
- [ ] NetworkPolicy for `postmortem-data`
- [ ] k3s `HelmChart` CRD bootstrap manifest

## Phase 3 — Recurrence & Evaluation ⭐

The phase that makes this more than a demo.

### 3.1 Recurrence memory
- [x] `compute_fingerprint()` — `(cause_type, effect_type, service, signature)`
      *(landed in Phase 1.1 — `memory.compute_fingerprint`)*
- [x] `find_similar_incidents()` — the Cypher in the graph doc
      *(also landed in 1.1; this phase is what finally exercises it)*
- [x] `CorrectiveAction` nodes + `REMEDIATED_BY`
      *(`memory.add_corrective_action` — new. Status is only ever set
      `ON CREATE`: re-extracting the same bullet from a later run must not
      silently reopen an action a human has since marked done)*
- [x] `nodes/recurrence.py` — inject `## Similar Past Incidents`
      *(code-rendered as a markdown table in `render/recurrence.py`, spliced
      into the document the same way the Timeline is — never handed to the
      Writer. Whether a past fix is still open is a structural fact,
      and asking a model to phrase it would be asking it to source a fact,
      which invariant 1 forbids. `nodes/recurrence.py` is the write half:
      it extracts the Writer's own "Corrective Actions" bullets from a
      *published* document and persists them as open actions, reusing the
      validator's section-exemption logic rather than a second parser)*
- [x] Surface **open** corrective actions from past matching incidents — this
      is the money feature ("third time; the March fix is still open")
- [x] `SIMILAR_TO` edges persisted post-run *(`memory.link_similar_incidents`,
      called from `cli.cmd_run` alongside the new
      `persist_corrective_actions` call — both only after a document passes,
      so a rejected draft's proposals never become graph facts)*
- [x] Golden incident #07 (recurrence of #01) passes

> **Verified two ways (2026-07-29).** Deterministic:
> `tests/integration/test_pipeline_recurrence.py` runs the actual DAG twice
> against the mock provider and asserts INC-0007's document names INC-0001.
> Real model, same fixtures: INC-0001 produced 4 open corrective actions;
> INC-0007's document opened its recurrence section with
> `**4 open corrective actions from a past matching incident**`, 100% overlap,
> all four named verbatim, root cause (the new commit) ranked #1 likely.
>
> Design note: incident 0007's fixture originally had **no** commit — the
> idea being "same symptom, no new code change." That produced a fingerprint
> component `log_error|alert_fired|...`, which cannot match INC-0001's
> `commit|log_error|...` components: `cause_type` is part of the match key, and
> a symptom-only incident has no commit to be a cause. Recurrence needs the
> *shape* to repeat, not just the symptom — so 0007 was rebuilt with its own
> commit reintroducing the same class of bug, which is also a more honest
> story ("the fix from six weeks ago never landed").

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
- [x] The side-by-side screenshot: postmortem citations ↔ Neo4j nodes
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
