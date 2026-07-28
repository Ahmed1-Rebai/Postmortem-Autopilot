# ADR-003 — Deterministic candidate generation, LLM ranking only

**Status:** Accepted · **Date:** 2026-07-28

## Context

The pipeline must go from ~5,000 raw evidence items to a ranked set of causal
hypotheses. The obvious LangGraph-shaped design is an agent with tools
(`query_logs`, `search_commits`, `read_graph`) that investigates freely until
it forms a conclusion. That is what most agentic demos do.

## Decision

Split the pipeline into three planes:

1. **Evidence plane** — deterministic collection, normalization, graph
   construction, and *candidate causal link generation*. No LLM.
2. **Reasoning plane** — LLM ranks the candidates and writes prose. It may
   only reference node IDs it was handed.
3. **Verification plane** — deterministic validation of every citation. No LLM.

Causal *recall* is code. Causal *precision* and phrasing are the model.

## Rationale

**Grounding becomes structural rather than aspirational.** If the model only
ever sees a fixed candidate set with real node IDs, and a validator resolves
every emitted ID against the graph, a fabricated citation is caught by a
database query. "The prompt says cite your sources" is not a guarantee; a
`MATCH` that returns null is.

**It's evaluable.** A free-roaming agent takes a different path every run.
The metrics in [07-evaluation.md](../07-evaluation.md) — precision@1,
decoy resistance, confidence separation — need reproducible behaviour to mean
anything. A fixed DAG with two LLM calls gives that.

**It's cheap and fast.** Two model calls per run, ~50–100k tokens, ~30 s. A
tool-calling agent doing the same investigation is 15–40 calls, minutes, and
an order of magnitude more cost. That difference decides whether the eval
suite can run nightly.

**Candidate generation is the wrong job for a model anyway.** "Find all pairs
where cause precedes effect within 15 minutes and they share a service" is a
loop. Handing it to an LLM makes it slower, more expensive, non-reproducible,
and no more accurate.

**Most of the system becomes unit-testable.** Collectors, normalizer, linker,
confidence model and validator are pure code with fixtures — no API key, no
network, no flake. Only ranking and prose need eval-based testing.

## Consequences

**Positive**
- Hallucinated citations are structurally preventable and measurably zero.
- Reproducible → evaluable → improvable.
- Low, predictable cost and latency.
- Small LLM surface: two prompts to maintain, not an agent loop to debug.

**Negative**
- **The system can only find causes its heuristics can generate candidates
  for.** If a real cause has no temporal, service or change-path signal, it's
  never a candidate, and the model can't rescue it. This is a genuine ceiling
  and it's the honest weakness of the design.
- Adding a new evidence type means writing a heuristic, not just widening a
  prompt.
- It's less impressive as an "autonomous agent" demo. It is more impressive as
  engineering, which is the intended audience.

**Mitigation for the ceiling**
Golden incident #05 (config change outside git — no evidence trail) exists
specifically to measure this. The correct behaviour there is *abstention with
a stated evidence gap*, not a guess. The system's response to "I can't
generate a candidate for the true cause" should be to say so, and that's a
tested behaviour rather than a hope.

Later, if warranted: a bounded "gap analysis" LLM step that looks at the
timeline and proposes *what evidence is missing* — without ever asserting a
cause. That extends coverage without breaking the grounding invariant.
