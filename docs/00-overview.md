# 00 — Project Overview

## One-line pitch

An evidence-grounded incident postmortem generator: a pipeline that
reconstructs an incident timeline from logs, alerts, deploys and chat, then
writes a postmortem where every claim is traceable to a timestamped source,
with competing hypotheses ranked by a transparent confidence model.

## Problem

After a production incident someone has to cross-reference alerts, logs,
recent deploys and Slack threads to write a postmortem. It takes hours, it's
tedious, and it is therefore skipped or done badly. The cost is
organizational: incidents repeat because nothing was written down, and what
was written down isn't trusted enough to act on.

The second-order problem is worse and is the one this project targets: **the
postmortems that do get written are unverifiable narratives.** "We believe the
cache eviction caused the latency spike" — based on what? Nobody re-checks.
An LLM writing that same sentence is strictly worse, because it's now a
confident guess with no provenance at all.

## What this does

1. **Collects evidence** for an incident window from logs, Prometheus alerts,
   git commits/deploys, and (optionally) chat threads.
2. **Builds a temporal knowledge graph** of those events in Neo4j, with
   deterministic temporal and *candidate* causal edges.
3. **Ranks competing hypotheses** — not one root cause, several, each scored
   by a documented confidence model, each with its disconfirming evidence
   listed.
4. **Writes the postmortem** with a citation tag on every factual sentence.
5. **Validates the output mechanically**: every citation must resolve to a
   real graph node, and citation coverage must clear a threshold, or the
   document is rejected and regenerated with the validator's complaints as
   feedback.
6. **Remembers**: each incident's causal pattern is fingerprinted, so a new
   incident can surface "this is structurally the same as INC-0042 (Mar 3),
   whose corrective action was never completed."

## Scope boundaries

**In scope (v1):** batch analysis of a closed incident window; four evidence
sources; markdown output; single-tenant; one Neo4j instance.

**Out of scope, explicitly:**
- Not an observability platform. It consumes Prometheus/Loki, doesn't replace
  them.
- Not real-time alerting or auto-remediation.
- Not a replacement for human review. Output is a **draft** with a confidence
  header telling the reviewer exactly which sections to distrust.
- No multi-tenancy, RBAC, or SSO. Those are enterprise concerns that add code
  without adding signal.

## Design principles

These are load-bearing. Every ADR traces back to one of them.

1. **The LLM never sources a fact.** Facts come from collectors, are stored in
   the graph, and are referenced by ID. The LLM selects, ranks, and phrases —
   it does not retrieve from its weights.
2. **If it can't be validated by code, it isn't a guarantee.** "The prompt
   says to cite sources" is not grounding. A validator that resolves every
   node ID against Neo4j is.
3. **Uncertainty is a first-class output.** A tentative conclusion clearly
   marked tentative is more useful than a confident wrong one.
4. **Deterministic where possible, probabilistic only where necessary.**
   Candidate generation is code. Ranking and prose are the LLM. This keeps
   token cost, latency, and the test surface small.
5. **Lightweight by default.** The whole system runs on one 4 GB node. Scale
   is a deployment parameter, not an architecture.

## Why this is CV-worthy

- It combines agentic AI with real infrastructure (k3s, Helm, Prometheus,
  NATS, graph modelling) rather than being an LLM wrapper with a chat box.
- The grounding-and-validation loop is a genuine engineering answer to
  hallucination, and it's measurable — see [07-evaluation.md](07-evaluation.md).
- It ships an **eval harness with numbers**: root-cause precision@1, citation
  coverage, hallucinated-citation rate. Very few portfolio AI projects can
  state how well they work. This one can.
- The comparison story is defensible: Rootly / incident.io / FireHydrant
  mostly summarize a chat thread. This cross-references structured infra
  events and shows its work.

## Talking about it honestly

In an interview, be able to say which parts are fully working and which are
proof-of-concept. See the scoping note in [04-tech-stack.md](04-tech-stack.md).
Interviewers consistently respect a clean "that part is a POC, here's what
production would need" over an overclaim they can puncture in two questions.
