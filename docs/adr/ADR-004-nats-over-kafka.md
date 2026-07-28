# ADR-004 — NATS JetStream instead of Kafka

**Status:** Accepted (Phase 4 — may never be built) · **Date:** 2026-07-28

## Context

The original stack listed Kafka (or Redpanda) for streaming log/alert
ingestion into the collector "in near-real-time instead of batch polling."

Worth being blunt about the requirement first: **this project does not
currently need a message broker.** It analyzes closed incident windows in
batch. A broker is justified only if the design moves to continuous ingestion
— maintaining a rolling event buffer so an incident window can be served
instantly rather than re-queried from Loki and GitHub each run.

## Decision

If streaming ingestion is built, use **NATS JetStream**. Not Kafka, not
Redpanda. And build it **only after** Phases 1–3 are complete.

## Rationale

| | NATS JetStream | Kafka (KRaft) | Redpanda |
|---|---|---|---|
| Binary | ~15 MB, single | JVM + ~600 MB image | ~200 MB, single |
| Idle RAM | ~20 MB | ~1 GB | ~350 MB |
| Config to "working" | one file | broker + topic + retention + partitions | moderate |
| Durable streams | yes | yes | yes |
| Consumer groups | yes | yes | yes |
| Ecosystem/tooling | smaller | largest | Kafka-compatible |

At this project's volume — a few thousand events per incident, a handful of
incidents per day — every option is functionally identical. The only
differentiator that matters is footprint, and Kafka's ~1 GB would nearly
double the steady-state budget for a component that idles.

Kafka is the more recognizable line on a CV. That's a real consideration and
it loses to a better one: *"I chose NATS because Kafka's JVM footprint was 40%
of my node budget for a workload doing thousands of messages per day"* is a
stronger interview answer than *"we used Kafka."* The first demonstrates
judgment; the second demonstrates a default.

## Consequences

**Positive**
- Streaming becomes affordable within the existing budget.
- Trivial to run: one binary, one config file, a `Deployment` and a PVC.
- Subject-based addressing (`evidence.logs.checkout`) maps cleanly to
  per-source, per-service streams.

**Negative**
- Less transferable brand recognition than Kafka.
- Smaller connector ecosystem — but this project writes its own producers
  anyway, so no connectors are needed.
- Fewer people on a team will have operated it.

## Note on sequencing

This is Phase 4 and explicitly optional. Adding a broker before the batch
pipeline works end-to-end would be infrastructure theatre — complexity that
looks like engineering while the thing being engineered doesn't exist yet.
If Phases 1–3 land and there's no time for this, ship without it; the project
is complete without a broker and doesn't need one to be defensible.
