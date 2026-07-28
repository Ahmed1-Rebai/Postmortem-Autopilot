# 01 — Architecture

## The core idea: three planes

The single most important structural decision in this system is that the LLM
is fenced into the middle third of the pipeline. Everything before it is
deterministic collection; everything after it is deterministic verification.

```
┌──────────────────────────────────────────────────────────────────┐
│ EVIDENCE PLANE — deterministic, no LLM, no tokens, fully testable │
│                                                                  │
│  Collectors ──► Normalizer ──► Graph Writer ──► Candidate Linker  │
│  (logs, alerts,  (Event        (Neo4j)          (time-window +    │
│   git, chat)      schema)                        service-overlap  │
│                                                  + signature      │
│                                                  heuristics)      │
└───────────────────────────────┬──────────────────────────────────┘
                                │  candidate hypotheses, each a set
                                │  of real node IDs. Nothing else.
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ REASONING PLANE — LangGraph, LLM calls                            │
│                                                                  │
│   Analyst ──────────────► Writer                                 │
│   ranks hypotheses,       renders the postmortem,                │
│   finds disconfirming     one citation tag per                   │
│   evidence                factual sentence                       │
│                                                                  │
│   Constraint: may only reference node IDs handed to it.          │
└───────────────────────────────┬──────────────────────────────────┘
                                │  draft markdown
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ VERIFICATION PLANE — deterministic, no LLM                        │
│                                                                  │
│   Citation Validator: every factual sentence has a tag?           │
│                       every tag resolves to a real Neo4j node?    │
│                       coverage ≥ threshold?                       │
│                       cited timestamp matches the node's?         │
│                                                                  │
│   PASS ──► publish        FAIL ──► feedback ──► Writer (max 2x)   │
│                                     then ──► fail loudly          │
└──────────────────────────────────────────────────────────────────┘
```

**Why this matters.** A hallucinated citation is not a style problem, it's a
correctness bug — and it is caught by a `MATCH (n {id: $id}) RETURN n` query,
not by a bigger model. This also means most of the system is unit-testable
without an API key, which keeps the eval loop fast and cheap.

## Pipeline stages

### 1. Collectors (evidence plane)

One collector per source, all implementing the same interface:

```python
class Collector(Protocol):
    name: str
    def collect(self, window: Window, service: str | None) -> list[RawRecord]: ...
```

| Collector | Phase 1 source | Phase 2+ source |
|---|---|---|
| `LogCollector` | local `.log` files / JSON lines | Loki `/loki/api/v1/query_range` |
| `AlertCollector` | mocked Alertmanager JSON fixture | Prometheus `/api/v1/query_range` + Alertmanager API |
| `GitCollector` | local repo via `git log` | GitHub API (PyGithub) |
| `DeployCollector` | derived from git tags / a `deploys.jsonl` fixture | k3s `Deployment` rollout history / Argo events |
| `ChatCollector` *(optional)* | Slack export `.json` | Slack Web API |

Collectors are **pure and offline-testable**. Every one has a fixture in
`evals/fixtures/`. A failing collector degrades the run (fewer evidence
sources → lower confidence scores) rather than aborting it — this is
deliberate and is reflected in the confidence model.

### 2. Normalizer (evidence plane)

Collapses every source into one `Event` shape. This is the contract the rest
of the system depends on:

```python
@dataclass(frozen=True)
class Event:
    id: str              # deterministic: sha256(source + native_id)[:16]
    type: EventType      # log_error | alert_fired | deploy | commit | chat_message
    timestamp: datetime  # UTC, always
    source: str          # "loki" | "alertmanager" | "github" | ...
    service: str | None  # normalized service name, for overlap heuristics
    signature: str | None  # normalized error fingerprint, see below
    summary: str         # one line, human-readable, safe to quote
    raw: dict            # untouched original payload
```

Two normalization jobs earn their keep:

- **Service name canonicalization.** `checkout-svc`, `checkout_service` and
  `checkout` are the same service. A small alias map (config, not magic)
  makes service-overlap a usable signal.
- **Error signature.** Strip UUIDs, hex, numbers, quoted strings and paths
  from a log message to get a fingerprint. `ConnectionError: pool exhausted
  (32/32)` and `(64/64)` become the same signature, so 4,000 log lines
  collapse into 6 distinct failure modes. This is what makes the graph
  readable and the token cost bounded.

IDs are **content-derived**, so re-running a collection is idempotent — no
duplicate nodes, and citations stay stable across reruns.

### 3. Graph Writer (evidence plane)

Writes `Event` nodes and *temporal* edges into Neo4j. Idempotent via `MERGE`
on the deterministic ID. Schema is in
[02-knowledge-graph.md](02-knowledge-graph.md).

### 4. Candidate Linker (evidence plane) — the interesting part

Generates `POSSIBLY_CAUSED` edges using three independent, cheap heuristics.
Each edge stores *which* heuristics fired, because that's what the confidence
model consumes downstream.

| Heuristic | Rule | Signal it provides |
|---|---|---|
| **Temporal proximity** | cause precedes effect by 0 < Δt ≤ 15 min (configurable) | weak on its own — everything correlates in time during an incident |
| **Service overlap** | cause and effect touch the same canonical service | medium |
| **Change-path overlap** | files touched by the commit map to the failing service/module | strong — this is the one that separates "a deploy happened" from "*this* deploy" |

Deliberately **no LLM here.** Generating candidates is a recall problem: be
generous, produce 20-50 candidate links, and let the ranking stage do
precision. Doing recall with an LLM would be slow, expensive, and
non-reproducible.

### 5. Analyst (reasoning plane, LangGraph node)

Input: the candidate subgraph, serialized compactly (node IDs + summaries +
which heuristics fired). Output: a ranked list of **competing hypotheses**.

```python
@dataclass
class Hypothesis:
    rank: int
    statement: str            # "Deploy abc123 removed DB_POOL_MAX, exhausting the pool"
    supporting_event_ids: list[str]
    contradicting_event_ids: list[str]   # required — may be empty, never unset
    confidence: float         # from the model in 03-confidence-model.md
    band: Literal["likely", "plausible", "tentative"]
```

Two rules make this section trustworthy:

- **Always produce ≥ 2 hypotheses** when ≥ 2 candidate chains exist. Forcing
  the model to name a runner-up surfaces the cases where it was guessing.
- **Disconfirming evidence is mandatory.** "What in this graph argues against
  this hypothesis?" is the question that catches confident nonsense. If
  nothing contradicts it *and* nothing corroborates it, that's a tentative
  score, not a strong one.

Confidence is **computed by code** from the heuristic flags and corroboration
counts. The LLM does not pick the number — it orders hypotheses and explains
them. See [03-confidence-model.md](03-confidence-model.md).

### 6. Writer (reasoning plane, LangGraph node)

Renders the postmortem. Sections: Summary, Impact, Timeline, Hypotheses
(ranked), Contributing Factors, Corrective Actions, Open Questions.

Every factual sentence carries a citation tag:

```markdown
The checkout service began returning 500s at 14:02:11 UTC
[src:a3f9c1e2b4d6, 2026-03-12T14:02:11Z].
```

The Timeline section is **rendered from the graph by code, not written by the
LLM** — it's a sorted list of events, and there's no reason to pay a model to
sort. This alone removes the largest hallucination surface in the document.

### 7. Citation Validator (verification plane)

Runs four checks:

| Check | Failure mode it catches |
|---|---|
| Every factual sentence has ≥ 1 `[src:...]` tag | model asserting an uncited claim |
| Every cited ID exists in Neo4j for this incident | fabricated or cross-incident citation |
| Cited timestamp matches the node's timestamp | plausible-looking but wrong attribution |
| Coverage ≥ 95% of factual sentences | slow drift toward narrative prose |

"Factual sentence" = a sentence that isn't in an allow-listed section
(Corrective Actions, Open Questions) and isn't a hedge/meta sentence. The
classifier is a small regex + heuristic ruleset, kept deliberately dumb and
inspectable; its own accuracy is measured in the eval suite.

On failure the validator returns *structured* complaints ("sentence 7 has no
citation; sentence 12 cites `9f2b...` which does not exist") and the Writer
retries with that feedback. **Bounded at 2 retries**, then the run fails
loudly. A pipeline that silently lowers its own standards is worse than one
that stops.

### 8. Recurrence Memory (evidence plane, post-run)

After a successful run, the incident's causal pattern is fingerprinted:
sorted tuple of `(cause_type, effect_type, service, error_signature)`. On the
next incident, a Cypher query finds past incidents with overlapping
fingerprints and injects `## Similar Past Incidents` into the draft —
including whether that incident's corrective actions were ever marked done.

This is the feature that makes the system worth running twice, and it's why
the graph is the right storage model rather than a folder of markdown files.

## LangGraph state

```python
class PipelineState(TypedDict):
    incident_id: str
    window: tuple[datetime, datetime]
    events: list[Event]
    graph_ready: bool
    candidates: list[CandidateLink]
    hypotheses: list[Hypothesis]
    similar_incidents: list[dict]
    draft_md: str
    validation: ValidationReport
    retry_count: int
    token_usage: dict[str, int]      # tracked per node, reported per run
```

Graph shape:

```
collect ─► build_graph ─► link_candidates ─► recall_similar ─► analyze
                                                                  │
                                                                  ▼
                                       ┌──────────────────────► write
                                       │                          │
                                  (retry, max 2)                  ▼
                                       │                      validate
                                       └──────── fail ────────────┤
                                                                  │ pass
                                                                  ▼
                                                              publish
```

Only `analyze` and `write` make LLM calls. That's **two model calls per run**
in the happy path — cheap, fast, and easy to reason about.

## Failure semantics

| Failure | Behaviour |
|---|---|
| A collector errors | log it, continue with fewer sources, record the gap in the run report, confidence scores drop accordingly |
| Zero events collected | abort — nothing to analyze, this is a config error not an incident |
| Neo4j unreachable | abort and retry the whole run (idempotent IDs make this safe) |
| LLM call fails / rate-limited | exponential backoff, 3 attempts, then abort |
| Validation fails 3× | write `draft_md` + `ValidationReport` to disk, exit non-zero, **do not publish** |

Every run emits a `RunReport` (sources used, sources that failed, event counts,
token usage, wall-clock, validation result). This is what the Grafana dashboard
in Phase 2 reads.

## What this design deliberately does not do

- **No agent free-roam / tool-calling loop.** The pipeline is a fixed DAG. An
  incident analyzer that can wander is one that can't be evaluated, and the
  eval harness is the point.
- **No vector store / RAG.** Retrieval here is structural (graph traversal
  over a known window), not semantic. Embeddings would add a dependency and a
  failure mode to solve a problem this system doesn't have. If "find similar
  incidents by prose" is ever wanted, that's when to revisit it.
- **No fine-tuning.** The task is constrained enough that a good prompt plus a
  hard validator gets there, and fine-tuning would destroy the ability to swap
  models.
