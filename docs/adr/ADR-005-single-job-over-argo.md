# ADR-005 — A single k3s Job, not Argo Workflows

**Status:** Accepted (Argo deferred, likely permanently) · **Date:** 2026-07-28

## Context

The original plan had Argo Workflows as a stretch goal: each pipeline stage
(collect → analyze → write → score) as a separate container in a DAG, so
stages are independently retryable and natively visible in Kubernetes.

## Decision

Run the whole LangGraph pipeline as **one k3s `Job`**. Keep Argo Workflows as
a documented, unbuilt option.

## Rationale

**The DAG is already observable — in LangGraph.** The stated benefit of Argo
was seeing pipeline structure natively in Kubernetes. But the structure is
already explicit in `graph.py`, and per-stage timing is already exported as
`pm_stage_duration_seconds{stage}` to Grafana. Argo would provide a second,
worse view of information already available.

**Retries are already handled at the right granularity.** The one stage that
genuinely needs retry is Writer→Validator, and that's a LangGraph edge with
bounded retries and structured feedback. Argo's retry is process-level: it
would re-run the whole Writer container without the validator's complaints,
which is strictly less useful.

**Four containers is four times the state-passing problem.** Separate
containers can't share Python objects, so `PipelineState` would have to be
serialized to Valkey or a PVC between every stage — new failure modes, new
serialization bugs, and a Redis dependency that's currently optional becoming
mandatory. That's real complexity bought for no capability.

**Cost.** Argo's controller and server are ~300–400 MB, plus CRDs, plus four
container starts (~5 s each) per run instead of one. Against a 2.4 GB budget
and a 30 s runtime, that's a meaningful regression.

**Runtime is 30 seconds.** DAG orchestrators earn their keep on
multi-hour pipelines with expensive stages worth checkpointing. At 30 s, a
failed run is simply re-run.

## Consequences

**Positive**
- Simpler deployment: one image, one Job template, one place to look at logs.
- No inter-stage serialization layer.
- ~400 MB and a set of CRDs stay out of the cluster.

**Negative**
- No per-stage container isolation — a memory leak in one stage affects the
  whole run. At this size, acceptable.
- No native Kubernetes-level DAG visualization. Mitigated by the LangGraph
  graph export and the Grafana stage-timing panel.
- One fewer recognizable tool on the CV.

## When to revisit

Genuinely revisit if any of these become true:

- Runtime exceeds ~10 minutes, making mid-pipeline checkpointing valuable.
- Stages develop conflicting dependencies needing separate images.
- Stages need to fan out — e.g. one collector container per evidence source
  running in parallel across many services.
- Multiple incidents must be processed concurrently with per-stage resource
  limits.

Until then, adding Argo would be résumé-driven development, and an interviewer
who asks "why Argo here?" gets a much better answer from this ADR than from a
DAG that didn't need to exist.
