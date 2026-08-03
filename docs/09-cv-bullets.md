# 09 — CV Bullets

The checklist asks for **2–3 CV bullets, past tense, quantified from real eval
numbers — never invented ones.** Everything below maps to a number that exists
in `evals/results/history.jsonl`, the Phase-gate tables in [TODO.md](../TODO.md),
or a live cluster run. Each bullet is written in the format the repo's own
results can back up. Pick one or two; don't list all three and dilute them.

---

## Bullet 1 — the guarantee (the project's differentiator)

> Built an LLM incident-postmortem pipeline that **cannot state an uncited
> fact**: every claim must cite a Neo4j node, and a deterministic validator
> resolves each citation against the graph — holding hallucinated citations at
> **exactly 0 across a 10-incident eval corpus** (precision@1 **1.00**,
> citation coverage **1.00**, enforced as a hard CI gate, not a prompt).

Real numbers behind it: the 10-case mock run in `history.jsonl`
(`2026-08-01T132951Z`) reports P@1 1.00, R@3 1.00, COV 1.000, HALL 0.000,
DECOY 1.00, ABST 1.00; the hard-gate thresholds are hallucinated = 0,
coverage ≥ 0.95, precision@1 ≥ 0.70 ([07-evaluation.md](07-evaluation.md)).

## Bullet 2 — the reasoning work (deterministic confidence)

> Designed a **deterministic confidence model** — availability-aware
> denominator, capped contradiction penalty, config-driven signal weights —
> that separated the true root cause from a planted decoy by **+0.34** on the
> hardest corpus case, where both the real cause and the decoy changed the
> *same service* in the same time window (confidence separation went from
> 0.00 to 0.34 after a de-duplication fix in the linker).

Real numbers behind it: the 0004-two-deploys gate table in
[TODO.md](../TODO.md) and the linker fix note under 1.8 / confidence note
under 1.4. The model itself is [03-confidence-model.md](03-confidence-model.md),
weighted in `config/confidence.yaml`, and 53 unit tests pin the expected
scores including both worked examples from the doc.

## Bullet 3 — the platform (it runs for real, not just in a notebook)

> Deployed the pipeline to **k3s as a Helm chart**: a **143 MB** non-root,
> read-only-rootfs image triggered by an Alertmanager webhook receiver that
> creates real k8s Jobs, with Prometheus/Alertmanager/Grafana, a nightly
> Neo4j backup CronJob, and a **tested restore round-trip** (21 nodes, 46
> relationships, exact match against the live graph).

Real numbers behind it: the in-cluster Job run reported
`INC-0001: PASS · coverage 100.0% · hallucinated 0 · 24.8s · 1836 tokens`
(checkpoint-1 note in [TODO.md](../TODO.md)); the image size is measured in
the Dockerfile's build log; the restore procedure is
[05-k3s-deployment.md](05-k3s-deployment.md).

---

## Supporting facts you can cite if asked

- Two model calls per run in the happy path (Analyst ranks, Writer phrases);
  candidate generation and citation validation are code.
- Signature collapsing folds ~4,000 log lines into 6 failure modes (150×
  reduction on a 3k-line fixture) — this is what keeps token cost bounded.
- The recurrence feature: INC-0007's document opened with "4 open corrective
  actions from a past matching incident", all four named verbatim from
  INC-0001 (100% overlap).
- 514 unit tests, 8 integration test files against real Neo4j + Valkey
  (testcontainers, not mocks), a gated mock eval on every PR, a real-provider
  eval nightly.

## What NOT to claim (the project's own honesty rule)

- **Don't** say the confidence score is a probability. It's an ordinal,
  decomposable score; the docs say exactly how it would be calibrated
  ([03-confidence-model.md](03-confidence-model.md)).
- **Don't** imply the eval numbers came from a real-model run. The 1.00s above
  are the mock structural run; the real-model runs (from the Phase-1/3 gate
  notes) are smaller in count and are the ones that say "correct root cause,
  1.00 likely".
- **Don't** claim multi-node or production scale. Single k3s node, single
  Neo4j, synthetic corpus — see [08-at-10x-scale.md](08-at-10x-scale.md) for
  the honest POC/production split.
