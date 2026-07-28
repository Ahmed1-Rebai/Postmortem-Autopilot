# 02 — Knowledge Graph & Memory

## Why a graph

The product promise is that every claim traces to a timestamped source. That
makes the primary query *"what chain of events connects this deploy to this
alert, and what else corroborates each hop?"* — a variable-depth traversal
over typed relationships. That's a graph query. In SQL it's a pile of
recursive CTEs; in a vector store the temporal and causal structure is simply
lost.

The second reason is recurrence: "have we seen this *pattern* before" is
subgraph matching, not text similarity.

## Two-tier memory

| Tier | Store | Holds | Lifetime |
|---|---|---|---|
| **Long-term** | Neo4j | incidents, events, causal edges, fingerprints, corrective-action status | permanent — this is the source of truth |
| **Short-term** | Valkey | in-flight pipeline state, collector caches, dedup sets, LLM response cache keyed by prompt hash | TTL 24h, disposable |

Valkey is **never** the source of truth. If Valkey is wiped mid-run, the run
restarts from the graph and produces identical output (content-derived IDs
make this true).

## Schema

### Nodes

| Label | Key properties | Notes |
|---|---|---|
| `Incident` | `id`, `title`, `start_time`, `end_time`, `severity`, `status`, `fingerprint` | the investigation unit |
| `Event` | `id`, `type`, `timestamp`, `source`, `service`, `signature`, `summary`, `raw_json` | base label — **every** evidence node also carries `:Event` |
| `Deploy` | + `commit_sha`, `service`, `version`, `rollout_status` | `:Event:Deploy` |
| `Alert` | + `rule_name`, `severity`, `labels`, `resolved_at` | `:Event:Alert` |
| `LogEntry` | + `level`, `message`, `count` | `:Event:LogEntry` — `count` because signatures collapse duplicates |
| `Commit` | + `sha`, `author`, `message`, `files_changed` | `:Event:Commit` |
| `ChatMessage` | + `author`, `channel`, `text` | `:Event:ChatMessage` |
| `Service` | `name`, `aliases` | canonical service, links events that share a service |
| `Hypothesis` | `id`, `statement`, `confidence`, `band`, `rank`, `created_at` | persisted so past reasoning is auditable |
| `CorrectiveAction` | `id`, `description`, `owner`, `status`, `due` | the thing that makes recurrence detection useful |

Dual-labelling (`:Event:Deploy`) means generic timeline queries hit `:Event`
while type-specific heuristics hit `:Deploy`. No polymorphism gymnastics.

### Relationships

| Relationship | From → To | Properties | Meaning |
|---|---|---|---|
| `PART_OF` | Event → Incident | — | evidence belongs to this incident's window |
| `PRECEDES` | Event → Event | `delta_seconds` | strict temporal ordering (consecutive events only, not the full transitive closure) |
| `AFFECTS` | Event → Service | — | this event concerns this service |
| `POSSIBLY_CAUSED` | Event → Event | `heuristics: list[str]`, `delta_seconds`, `score` | **candidate** causal link from the linker |
| `CORROBORATES` | Event → Event | `basis` | two independent sources supporting the same fact |
| `CONTRADICTS` | Event → Hypothesis | `reason` | disconfirming evidence — as first-class as support |
| `SUPPORTS` | Event → Hypothesis | — | the citation backbone |
| `EXPLAINS` | Hypothesis → Incident | `confidence`, `accepted_by`, `accepted_at` | a hypothesis promoted to accepted root cause |
| `REMEDIATED_BY` | Incident → CorrectiveAction | — | closes the loop for recurrence checks |
| `SIMILAR_TO` | Incident → Incident | `overlap`, `matched_on` | computed post-run from fingerprints |

Note `CONTRADICTS` and `SUPPORTS` point at `Hypothesis`, not at `Incident`.
Evidence supports *a claim*, not an incident — keeping that distinction in the
schema is what allows two competing hypotheses to coexist with their own
evidence sets.

## Constraints & indexes

Run once at startup (all idempotent):

```cypher
CREATE CONSTRAINT event_id     IF NOT EXISTS FOR (e:Event)     REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT incident_id  IF NOT EXISTS FOR (i:Incident)  REQUIRE i.id IS UNIQUE;
CREATE CONSTRAINT service_name IF NOT EXISTS FOR (s:Service)   REQUIRE s.name IS UNIQUE;
CREATE CONSTRAINT hypo_id      IF NOT EXISTS FOR (h:Hypothesis) REQUIRE h.id IS UNIQUE;

CREATE INDEX event_ts   IF NOT EXISTS FOR (e:Event) ON (e.timestamp);
CREATE INDEX event_sig  IF NOT EXISTS FOR (e:Event) ON (e.signature);
CREATE INDEX event_type IF NOT EXISTS FOR (e:Event) ON (e.type);
```

The uniqueness constraint on `Event.id` plus content-derived IDs is what makes
the whole ingest path idempotent — re-running a collector `MERGE`s onto the
same node instead of duplicating evidence and inflating corroboration counts.
(That inflation would silently corrupt confidence scores, which is the sort of
bug that's very hard to notice.)

## Queries that matter

**Rank candidate causes by corroboration**

```cypher
MATCH (cause:Event)-[r:POSSIBLY_CAUSED]->(effect:Event)-[:PART_OF]->(i:Incident {id: $incidentId})
OPTIONAL MATCH (cause)-[:POSSIBLY_CAUSED]->(other:Event)-[:PART_OF]->(i)
WHERE other <> effect
WITH cause, effect, r,
     count(DISTINCT other)            AS breadth,
     size(r.heuristics)               AS heuristic_count,
     collect(DISTINCT other.type)     AS effect_types
RETURN cause.id, cause.summary, effect.id, effect.summary,
       r.heuristics, r.delta_seconds, breadth, heuristic_count, effect_types
ORDER BY heuristic_count DESC, breadth DESC, r.delta_seconds ASC
LIMIT 15;
```

**Full timeline (rendered by code, not the LLM)**

```cypher
MATCH (e:Event)-[:PART_OF]->(:Incident {id: $incidentId})
RETURN e.timestamp, e.type, e.service, e.summary, e.id, e.count
ORDER BY e.timestamp ASC;
```

**Citation resolution — the validator's core query**

```cypher
UNWIND $citedIds AS cid
OPTIONAL MATCH (e:Event {id: cid})-[:PART_OF]->(:Incident {id: $incidentId})
RETURN cid, e IS NOT NULL AS valid, e.timestamp AS actual_ts;
```

One round trip validates the entire document, and it checks both existence
*and* incident membership — a citation to a real node from a different
incident is still a fabrication.

**Recurrence: structurally similar past incidents**

```cypher
MATCH (i:Incident {id: $incidentId})
MATCH (past:Incident)
WHERE past.id <> i.id AND past.end_time < i.start_time
WITH i, past,
     [x IN i.fingerprint WHERE x IN past.fingerprint] AS shared
WHERE size(shared) >= 2
OPTIONAL MATCH (past)-[:REMEDIATED_BY]->(ca:CorrectiveAction)
RETURN past.id, past.title, past.end_time, shared,
       toFloat(size(shared)) / size(i.fingerprint) AS overlap,
       collect({action: ca.description, status: ca.status}) AS actions
ORDER BY overlap DESC
LIMIT 5;
```

That last `collect` is the payoff: *"this is the third time; the fix from
March is still open."* No other part of the system can produce that sentence.

## `memory.py` — the interface agents use

Agent code never writes Cypher. One module owns the graph:

```python
# --- schema lifecycle -------------------------------------------------
def ensure_schema() -> None: ...

# --- write path (evidence plane) --------------------------------------
def upsert_incident(incident: Incident) -> str: ...
def write_events(incident_id: str, events: list[Event]) -> list[str]: ...
def link_temporal(incident_id: str) -> int: ...          # builds PRECEDES chain
def link_candidate(cause_id: str, effect_id: str,
                   heuristics: list[str], delta_seconds: int) -> None: ...

# --- read path (reasoning plane) --------------------------------------
def get_timeline(incident_id: str) -> list[Event]: ...
def get_candidate_chains(incident_id: str, limit: int = 15) -> list[Chain]: ...
def find_similar_incidents(incident_id: str, min_shared: int = 2) -> list[dict]: ...

# --- verification plane ------------------------------------------------
def resolve_citations(incident_id: str,
                      cited_ids: list[str]) -> dict[str, CitationCheck]: ...

# --- post-run ----------------------------------------------------------
def persist_hypotheses(incident_id: str, hypotheses: list[Hypothesis]) -> None: ...
def compute_fingerprint(incident_id: str) -> list[str]: ...
def link_similar_incidents(incident_id: str) -> int: ...
```

Design rules for this module:

- **Every function takes `incident_id`.** There is no ambient context, and
  cross-incident leakage is therefore structurally impossible.
- **Returns dataclasses, never raw `neo4j.Record`.** The driver type must not
  escape this file, or swapping the graph backend later becomes a rewrite.
- **All writes idempotent** (`MERGE` on deterministic IDs).
- **No LLM imports.** This file is pure data access and is tested against a
  real Neo4j in Docker, not a mock.

## Retention

Graph growth is bounded by an incident-count policy, not a time policy:
keep the last 200 incidents fully, then downsample older ones to
`Incident` + `Hypothesis` + `CorrectiveAction` + `fingerprint`, dropping raw
`Event` nodes. Recurrence detection only needs fingerprints, so old incidents
keep contributing to institutional memory at ~1% of the storage. Run as a
weekly k3s CronJob (Phase 3).
