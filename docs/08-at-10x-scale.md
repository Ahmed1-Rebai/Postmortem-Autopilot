# 08 — What I'd Do Differently at 10× Scale

This is a written answer, not a sales pitch. The honest answer is that a lot
of what's here was built to be *demonstrable on one 4 GB node* (the selection
criterion in [04-tech-stack.md](04-tech-stack.md)), and 10× of the wrong
metric is the fastest way to expose which parts were POC and which were real.
This section names the pressure points and what I'd change, in rough order of
how soon they'd hurt.

## What "10×" means here

- **10× incidents/day** → 10× pipeline Jobs, 10× eval volume, concurrency
  instead of one-at-a-time.
- **10× evidence volume per incident** → signature collapsing and graph
  serialization to the Analyst stop being free.
- **Multiple teams/services** → per-service alias maps and a repo-wide
  `config/services.yaml` stop being the right granularity.

## Things that break first

1. **Neo4j Community, single instance, offline-only backups.** The nightly
   `neo4j-admin dump` (dump → scale to 0 → restore) already scales the
   StatefulSet to zero because Community Edition has no online dump. At 10×
   the dump window grows and the restore RTO is hours. The documented escape
   hatch is real: [ADR-002](adr/ADR-002-neo4j-over-alternatives.md) covers
   moving the *per-incident* working graph to embedded Kùzu and keeping Neo4j
   only for cross-incident recurrence queries. At 10× I'd take that trade —
   most graph queries here are per-incident, and Kùzu makes an incident
   self-contained (and trivially disposable after retention).

2. **Pushgateway as the metrics carrier.** Per-incident-labeled snapshots work
   at this scale because the pipeline is ephemeral and there's nothing to
   scrape. The retained set grows unbounded and the self-alerts are
   `count()`/`sum()` over it (a stated limitation, not a hidden one). At 10× I
   would move the run report into the graph itself (it already is, in the
   `RunReport` JSON and graph) and let Prometheus scrape a small long-lived
   exporter, or push to a real metrics backend with native retention. The
   Pushgateway approach is correct *for an ephemeral Job*; it is not a
   substitute for a time-series backend at volume.

3. **Unbounded evidence into the LLM.** Signature collapsing caps tokens today
   (6 failure modes from 4,000 log lines), but the Analyst input is still the
   whole serialized candidate subgraph. At 10× I'd bucket evidence by
   signature and window and cap the per-signature payload, so token cost
   scales with *failure modes* rather than log volume — the same discipline as
   the collapse, one level up. This is the concrete change I'd make before any
   of the infrastructure ones.

4. **The factual-sentence classifier is deliberately dumb.** It's regex +
   section allow-list, and its accuracy is measured in the eval suite rather
   than assumed. That's the right shape, but a single classifier for every
   team's style drifts. At 10× I'd report classification accuracy per
   incident/team in the eval output and make the classifier data-driven
   (a labelled sentence corpus), not more clever heuristics.

5. **One Job per incident, retry budget 2.** A queue with per-stage retries
   (NATS JetStream, [ADR-004](adr/ADR-004-nats-over-kafka.md)) is the natural
   fix for concurrency, backpressure, and webhook durability when Jobs can't
   start instantly. Until then, single-Job stays right — a workflow engine
   ([ADR-005](adr/ADR-005-single-job-over-argo.md)) adds scheduling machinery
   to fix a problem this project doesn't have yet. Same shape of decision,
   later threshold.

6. **The eval corpus is 10 synthetic incidents.** The metrics are real; the
   sample is small (stated in docs/04). At 10× the first investment is
   labelled *real* incidents, because the confidence model is explicitly an
   ordinal, decomposable score — not a calibrated probability
   ([03-confidence-model.md](03-confidence-model.md)). With a real corpus I'd
   bucket predictions by score, fit isotonic regression, and set the
   likely/plausible/tentative band boundaries from observed accuracy instead
   of the hand-picked 0.75/0.40 thresholds.

## What already scales (deliberately)

- **Content-derived IDs + `MERGE`** — idempotency is the load-bearing
  invariant, and it's why a retry, a sweep, or a re-run never duplicates
  evidence or corrupts confidence. This is the property I'd defend in any
  redesign, not the storage choice.
- **Deterministic candidate generation** — recall-with-code is parallelizable
  and reproducible; an LLM generating candidates wouldn't be.
- **Prompts as a ConfigMap, not baked into the image** — a prompt change is a
  `helm upgrade`, no rebuild. At 10× I'd add hash-pinning and an eval-diff
  gate on prompt changes, but the mechanism is already the right one.
- **Metrics and eval trend in `history.jsonl`** — the data to justify or
  reject the changes above already exists.

## POC vs. production-grade, in one table

For the "be ready to say which parts are POC" checklist item — the full
scoping note is in [04-tech-stack.md](04-tech-stack.md):

| Part | Grade | The gap to production |
|---|---|---|
| Evidence → graph → hypotheses → validated postmortem | **Production-grade, locally** | Single-node, single-tenant; the eval corpus is small |
| Citation validation (resolve against Neo4j) | **Production-grade** | This is the core invariant; nothing else compensates if it weakens |
| k3s deployment, receiver, observability, backup+restore | **Production-grade, verified on a live cluster** | Single-node k3d/k3s; multi-node needs networking + storage changes |
| NetworkPolicy | **Correct YAML, unverified enforcement** | k3d's Flannel doesn't enforce it; verified-correct on bare-metal k3s |
| Live collectors (Loki/Prometheus/GitHub APIs) | **POC** | Phase 4, not built; local fixtures stand in |
| NATS, OTel, Flux, Argo | **Not built** | Explicitly deferred — see the ADRs before anyone claims otherwise |

The useful summary: the *guarantee* (a claim can't ship without a resolvable
citation) and the *measurement* (eval gates) are production-grade. The *scale
story* (multi-node, multi-tenant, live sources) is POC, and this document is
the plan for closing that gap.
