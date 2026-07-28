# 03 — Confidence Model

The original design used `confidence = corroborating_sources /
total_possible_sources`. That's a reasonable first instinct but it has three
failure modes worth naming, because the fix is what makes this section
defensible in an interview:

1. **It treats all evidence as equal.** A commit touching the exact failing
   module is far stronger evidence than "a deploy happened 9 minutes earlier,"
   yet both count as 1.
2. **It punishes missing sources.** If Slack isn't wired up, `total_possible`
   still counts chat, so every score is capped below 1 for a reason unrelated
   to the evidence.
3. **It ignores contradiction.** Evidence *against* a hypothesis doesn't move
   the number at all.

## The model

```
                Σ (wᵢ · sᵢ)  for i in signals present and available
raw_support  =  ─────────────────────────────────────────────────
                Σ  wᵢ        for i in signals AVAILABLE this run

confidence   =  clamp(raw_support − contradiction_penalty, 0.0, 1.0)
```

### Signal weights

| Signal `i` | Weight `wᵢ` | `sᵢ` = 1 when |
|---|---|---|
| `change_path_overlap` | **0.30** | files in the commit map to the failing service/module |
| `log_signature_match` | **0.25** | an error signature appears after the cause and not before it in the baseline window |
| `metric_correlation` | **0.20** | a Prometheus alert for the same service fires inside the window |
| `temporal_proximity` | **0.15** | cause precedes effect within the configured window |
| `human_confirmation` | **0.10** | a chat message names the cause or the fix |

Weights encode a defensible claim: *evidence that a specific change touched
the specific failing code is worth twice as much as evidence that something
happened at roughly the right time.* They are config, not constants — they
live in `config/confidence.yaml` and the eval suite reports scores under
alternative weightings so the choice can be justified with numbers rather
than taste.

### Availability-aware denominator

The denominator only sums signals that were **actually collectible this run**.
No Slack integration → `human_confirmation` leaves the denominator entirely,
rather than acting as a permanent penalty. The set of available signals is
recorded in the `RunReport` and printed in the postmortem header, so a reader
knows a 0.8 was computed from three sources rather than five.

### Contradiction penalty

```
contradiction_penalty = min(0.4, 0.15 × n_contradicting_events)
```

Capped at 0.4 so contradicting evidence can *demote* a hypothesis but never
zero it out — a hypothesis with strong support and one contradiction is
genuinely still in play, and that ambiguity should reach the human reader.

### Bands

| Confidence | Band | How the postmortem renders it |
|---|---|---|
| ≥ 0.75 | **likely** | stated directly, with citations |
| 0.40 – 0.75 | **plausible** | hedged: "evidence suggests…", listed with alternatives |
| < 0.40 | **tentative** | explicitly flagged "tentative — needs human verification", never presented as the conclusion |

If the top hypothesis is `tentative`, the document opens with a banner saying
the analysis was inconclusive and naming what evidence would resolve it. A
system that admits this is more useful than one that always picks a winner.

## What this number is and isn't

**It is** a transparent, reproducible weighted score over named heuristics.
Given the same graph you get the same number, and any number can be decomposed
into which signals fired.

**It is not** a calibrated probability. A 0.8 does not mean "correct 80% of
the time." Claiming otherwise would require a labelled incident corpus far
larger than this project has.

Say exactly that in an interview. "It's an ordinal, decomposable score, not a
calibrated probability — calibrating it would need N labelled incidents and
here's how I'd collect them" is a much stronger answer than defending a
probability claim you can't support.

The path to calibration, if the corpus ever exists: bucket predictions by
score, plot observed accuracy per bucket, fit isotonic regression. The eval
harness already records everything needed to do this — see
[07-evaluation.md](07-evaluation.md).

## Worked example

Incident: checkout 500s after a deploy. Sources available: logs, alerts, git.
Chat not configured → `human_confirmation` excluded.

Available denominator: `0.30 + 0.25 + 0.20 + 0.15 = 0.90`

**H1 — "Deploy `abc123` dropped `DB_POOL_MAX`, exhausting the connection pool"**

| Signal | Fired | Contribution |
|---|---|---|
| change_path_overlap | ✅ commit edits `config/database.py` | 0.30 |
| log_signature_match | ✅ `pool exhausted` first seen 40s post-deploy | 0.25 |
| metric_correlation | ✅ `HighErrorRate{service=checkout}` fired | 0.20 |
| temporal_proximity | ✅ Δt = 40s | 0.15 |

raw_support = 0.90 / 0.90 = **1.00**, no contradictions → **1.00, likely**

**H2 — "Upstream payment-gateway latency caused the failures"**

| Signal | Fired | Contribution |
|---|---|---|
| change_path_overlap | ❌ | 0 |
| log_signature_match | ✅ some gateway timeouts present | 0.25 |
| metric_correlation | ❌ no alert on payment-gw | 0 |
| temporal_proximity | ✅ within window | 0.15 |

raw_support = 0.40 / 0.90 = 0.44; one contradicting event (gateway p99 flat
throughout) → penalty 0.15 → **0.29, tentative**

Both appear in the postmortem. H2 is explicitly marked tentative with its
contradicting evidence cited. A reviewer who happens to know the gateway was
being migrated that week has exactly what they need to overrule the ranking —
which is the whole point of showing the runner-up.
