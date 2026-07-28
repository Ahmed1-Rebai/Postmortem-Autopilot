# ADR-002 — Neo4j as the graph store

**Status:** Accepted (with a documented escape hatch) · **Date:** 2026-07-28

## Context

The system needs to store incident events and traverse causal chains, and to
match structurally similar past incidents. Neo4j is ~40% of the memory budget
(1 GB of 2.4 GB steady state) — by far the heaviest component in a project
whose stated principle is lightweight.

## Decision

Use **Neo4j 5 Community**, tuned down (512 MB heap, 256 MB pagecache). Keep
all Cypher confined to `agent/memory.py` so the backend is replaceable.

## Rationale

Why a graph at all, first — the alternative is Postgres with recursive CTEs:

- The primary query is variable-depth traversal over typed edges. Cypher
  expresses it in five lines; SQL needs recursive CTEs that nobody will read
  in six months.
- Recurrence detection is subgraph pattern matching. In SQL that's a
  self-join maze.
- The graph is *demoable*. Opening Neo4j browser next to the generated
  postmortem and showing that each citation is a real node is the single most
  persuasive thing this project can do in an interview. A Postgres table does
  not have that property.

Why Neo4j specifically over lighter graph stores:

- Cypher is the graph query language people recognize; it transfers.
- The Python driver is mature, the browser is genuinely good for debugging,
  and the docs are excellent.
- Community Edition is sufficient — no clustering needed.

## The escape hatch

If the 1 GB proves too much (e.g. deploying to a 2 GB VM), **Kùzu** is the
swap: embedded, Cypher-compatible, ~50 MB, no server process.

What that costs:
- No browser — the demo advantage is lost. This is the main reason it isn't
  the default.
- Embedded means the graph lives in the pipeline pod, so a k3s Job would need
  the DB on a shared PVC with single-writer discipline.
- Less familiar name on a CV.

Because `memory.py` is the only file containing Cypher and returns
dataclasses rather than driver types, this swap is a few hundred lines, not a
rewrite. That containment is the actual decision being made here — the
backend choice is reversible by construction.

## Consequences

**Positive**
- Best-fit query model; excellent debugging and demo story.
- Constraints and indexes give idempotent ingest cheaply.

**Negative**
- Largest single memory consumer; JVM, so it starts slowly (~20–30 s) and the
  compose healthcheck has to account for it.
- Community Edition has no clustering, no incremental backup, no PITR.
  Backup is a nightly `neo4j-admin dump` CronJob, and the restore path must be
  tested at least once.
- Single instance = a single point of failure. Acceptable for a batch tool
  where a failed run is retried, not acceptable for anything user-facing. Say
  it plainly rather than letting it be found.
