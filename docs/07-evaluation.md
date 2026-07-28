# 07 — Evaluation

The eval harness is not a nice-to-have. It is the thing that turns "I built an
AI incident tool" into "I built an AI incident tool that identifies the correct
root cause in 8 of 10 golden incidents with 97% citation coverage and zero
hallucinated citations." One of those sentences survives follow-up questions.

## Golden incidents

Each case is a directory with fixtures and a label:

```
evals/incidents/inc-0001-missing-env-var/
├── meta.yaml           # window, services, severity, description
├── logs/app.log        # realistic volume: thousands of lines, mostly noise
├── alerts.json         # Alertmanager-shaped
├── repo/               # a real git repo with real commits in the window
└── expected.yaml       # THE LABEL
```

```yaml
# expected.yaml
root_cause_event_id: "commit:a3f9c1e2"
root_cause_summary: "Deploy removed DB_POOL_MAX; pool exhausted under load"
acceptable_alternates: []              # other event IDs scored as correct
must_cite_events:                      # facts the doc must not omit
  - "alert:HighErrorRate:1710252131"
  - "log:sig:pool_exhausted"
must_not_conclude:                     # known decoys the model must resist
  - "payment gateway latency"
  - "database failover"
min_confidence: 0.70
```

`must_not_conclude` is the important field. Every incident is seeded with a
**plausible decoy** — an unrelated deploy in the window, a coincidental
latency blip, a red-herring error that predates the incident. Without decoys
the eval measures nothing; any system that picks the only candidate scores
100%.

### The starting corpus (10 cases)

| # | Scenario | Tests |
|---|---|---|
| 01 | Deploy drops an env var → pool exhaustion | happy path, strong change-path signal |
| 02 | Slow memory leak, deploy 6h before symptoms | temporal window too narrow — should score *tentative*, not wrong |
| 03 | Upstream third-party outage, no internal change | must conclude "no internal cause" rather than blame a random deploy |
| 04 | Two deploys in-window, only one causal | discrimination — the decoy case |
| 05 | Config change outside git (manual kubectl edit) | evidence genuinely absent → must say so, not invent |
| 06 | Cascading failure across three services | multi-hop chains, service overlap |
| 07 | Recurrence of #01 six weeks later | recurrence memory must surface #01 |
| 08 | Alert fires, no user impact (false positive) | must not manufacture an impact section |
| 09 | Rollback mid-incident | causal chain with a remediation event inside the window |
| 10 | Sparse evidence: alerts only, no logs | availability-aware denominator must adjust, not penalize |

Cases 03, 05, 08 and 10 are the valuable ones. They measure whether the system
**declines to conclude**, which is exactly what an ungrounded LLM won't do.

## Metrics

| Metric | Definition | Target |
|---|---|---|
| **Root-cause precision@1** | top hypothesis matches `root_cause_event_id` or an alternate | ≥ 0.70 |
| **Root-cause recall@3** | correct cause appears in top 3 | ≥ 0.90 |
| **Citation coverage** | factual sentences with ≥1 valid citation ÷ factual sentences | ≥ 0.95 |
| **Hallucinated-citation rate** | cited IDs not resolving to a node in this incident ÷ all citations | **0.00** |
| **Decoy resistance** | runs where no `must_not_conclude` item appears in the top hypothesis | ≥ 0.90 |
| **Abstention correctness** | on cases 03/05/08/10, top band is `tentative` and the banner fires | ≥ 0.75 |
| **Must-cite recall** | `must_cite_events` present in the doc ÷ total | ≥ 0.90 |
| **Confidence separation** | mean confidence(correct) − mean confidence(incorrect) | > 0.20 |
| **Cost per run** | total tokens × price | tracked, no target |
| **Latency p50/p95** | wall clock end-to-end | tracked, no target |

**Hallucinated-citation rate has a target of exactly zero** and it's a hard CI
gate. It's not a quality metric to improve — it's the invariant the whole
architecture exists to guarantee. If it's ever non-zero, the verification plane
has a bug.

**Confidence separation** is the subtle one: it asks whether the score is
*informative*. If wrong answers score as high as right ones, the number is
decoration.

## Running

```bash
python -m evals.runner --all                      # full suite
python -m evals.runner --case inc-0004 -v         # one case, verbose
python -m evals.runner --all --model claude-opus-5
python -m evals.runner --all --sweep-weights      # confidence weight sensitivity
python -m evals.runner --all --mock               # structural only, no tokens
```

Output: `evals/results/<timestamp>/` with per-case JSON, a summary table, and
a markdown report suitable for pasting into the README.

```
CASE                          P@1   COV    HALL   DECOY  CONF   BAND
inc-0001-missing-env-var       ✓   1.000   0.000    ✓    0.94   likely
inc-0002-slow-leak             ✓   0.973   0.000    ✓    0.51   plausible
inc-0003-upstream-outage       —   0.981   0.000    ✓    0.22   tentative  ← correct abstention
inc-0004-two-deploys           ✓   1.000   0.000    ✓    0.81   likely
...
────────────────────────────────────────────────────────────────────
P@1 0.80 │ R@3 0.90 │ COV 0.981 │ HALL 0.000 │ DECOY 1.00 │ ABST 0.75
Tokens 148k │ Cost $0.71 │ p50 34s │ p95 61s
```

## CI gates

`evals-quick` runs on every PR (mock provider, structural checks only, free
and fast). The full suite runs nightly and on `main`, gated at:

| Gate | Threshold | On breach |
|---|---|---|
| Hallucinated citations | `== 0` | **fail** |
| Citation coverage | `≥ 0.95` | **fail** |
| Root-cause precision@1 | `≥ 0.70` | **fail** |
| Decoy resistance | `≥ 0.90` | warn |
| Cost per run | `≤ $0.15` | warn |

Results are committed to `evals/results/history.jsonl`, so metric trends over
time are plottable. Showing an interviewer a graph of precision@1 improving
across prompt revisions is a materially different conversation from showing
them a repo.

## Building the corpus

Golden incidents are expensive to write by hand. Two shortcuts that keep them
honest:

1. **Generate the fixtures from a real repo.** Take any open-source service,
   pick a real commit that introduced a bug, and synthesize the logs/alerts
   that bug would have produced. The commits and file paths are real, so the
   change-path heuristic is exercised against real code structure.
2. **Mine your own history.** If you've been on-call anywhere, anonymized real
   incidents are the highest-value cases. Two or three real ones anchor the
   synthetic set.

Anonymize service names, hostnames and identifiers before committing.

## Limitations — state these before you're asked

- **N=10 is small.** These are directional numbers, not statistical claims. A
  single case flipping moves precision@1 by 10 points.
- **Mostly synthetic.** Fixtures are constructed, so they're cleaner than real
  logs. Real-world performance would be worse, particularly for signature
  extraction against genuinely messy multiline output.
- **Labels are single-author.** "The root cause" was decided by one person
  (you). Real incidents often have contested causes, and there's no
  inter-annotator agreement here.
- **Confidence is uncalibrated.** See [03-confidence-model.md](03-confidence-model.md).
  The harness records what calibration would need; it doesn't perform it.

Volunteering these limits is a stronger move than being walked into them. It
signals you know what a real evaluation would require, which is most of what
the question is actually probing.
