"""Per-case and per-suite scoring, per docs/07-evaluation.md's metric table.

Every function here is pure: given already-collected pipeline outputs (a
`Hypothesis` list, a `ValidationReport`, the rendered document, a
`RunReport`) and a parsed `expected.yaml`, it returns a typed result. No I/O,
no LLM calls — the orchestration and file I/O live in `evals/runner.py`.

`citation_coverage`/`hallucinated_citation_rate` are read straight off
`ValidationReport.coverage`/`.hallucination_rate` (`agent/state.py`) — the
pipeline's own verification plane already computed them; this module never
recomputes what invariant 2 already validated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from agent.nodes.validator import CITATION
from agent.state import Event, Hypothesis, ValidationReport
from evals.labels import UnresolvedLabelError, resolved_ids

# ---------------------------------------------------------------------------
# the label
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedLabel:
    """One golden incident's `expected.yaml`, parsed."""

    case_id: str
    root_cause_event_id: str
    root_cause_summary: str
    acceptable_alternates: tuple[str, ...]
    must_cite_events: tuple[str, ...]
    must_not_conclude: tuple[str, ...]
    min_confidence: float
    #: Not in docs/07's original schema. Which cases (03/05/08/10) must
    #: abstain — top hypothesis band == "tentative". Explicit per-fixture
    #: rather than inferred from the case number, since "should abstain" is
    #: a labeling decision, not something derivable from the other fields.
    is_abstention_case: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> ExpectedLabel:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            case_id=path.parent.name,
            root_cause_event_id=str(raw["root_cause_event_id"]),
            root_cause_summary=str(raw.get("root_cause_summary", "")),
            acceptable_alternates=tuple(raw.get("acceptable_alternates") or ()),
            must_cite_events=tuple(raw.get("must_cite_events") or ()),
            must_not_conclude=tuple(raw.get("must_not_conclude") or ()),
            min_confidence=float(raw.get("min_confidence", 0.0)),
            is_abstention_case=bool(raw.get("is_abstention_case", False)),
        )


# ---------------------------------------------------------------------------
# per-case metrics
# ---------------------------------------------------------------------------
def correct_event_ids(
    expected: ExpectedLabel, events: Sequence[Event]
) -> frozenset[str]:
    """The real IDs that count as correct for precision/recall: the root
    cause plus any acceptable alternates."""
    pseudo_ids = (expected.root_cause_event_id, *expected.acceptable_alternates)
    return resolved_ids(pseudo_ids, events)


def precision_at_1(
    hypotheses: Sequence[Hypothesis], correct_ids: frozenset[str]
) -> bool:
    if not hypotheses or not correct_ids:
        return False
    top = min(hypotheses, key=lambda h: h.rank)
    return bool(correct_ids & set(top.supporting_event_ids))


def recall_at_3(hypotheses: Sequence[Hypothesis], correct_ids: frozenset[str]) -> bool:
    if not correct_ids:
        return False
    for hyp in sorted(hypotheses, key=lambda h: h.rank)[:3]:
        if correct_ids & set(hyp.supporting_event_ids):
            return True
    return False


def decoy_resistance(hypotheses: Sequence[Hypothesis], expected: ExpectedLabel) -> bool:
    """True if the top hypothesis's statement references no known decoy — a
    case-insensitive substring heuristic, not a semantic check. There's no
    event a decoy phrase resolves to (it's "must not conclude X", not "must
    not cite event X"), so this is the honest ceiling on what a deterministic
    checker can verify here."""
    if not hypotheses or not expected.must_not_conclude:
        return True
    top = min(hypotheses, key=lambda h: h.rank)
    statement = top.statement.lower()
    return not any(decoy.lower() in statement for decoy in expected.must_not_conclude)


def abstention_correctness(
    hypotheses: Sequence[Hypothesis], expected: ExpectedLabel
) -> bool | None:
    """`None` when the case isn't an abstention case — the caller averages
    this metric only over cases where it returns a real bool, matching
    docs/07's "on cases 03/05/08/10" scoping."""
    if not expected.is_abstention_case:
        return None
    if not hypotheses:
        return True
    top = min(hypotheses, key=lambda h: h.rank)
    return top.band == "tentative"


def must_cite_recall(
    document_md: str, expected: ExpectedLabel, events: Sequence[Event]
) -> tuple[float, tuple[str, ...]]:
    """Fraction of `must_cite_events` present as a citation in the rendered
    document. Returns the fraction plus any pseudo-IDs that failed to
    resolve at all (a fixture bug, reported rather than silently dropped)."""
    if not expected.must_cite_events:
        return 1.0, ()

    required_ids: set[str] = set()
    unresolved: list[str] = []
    for pseudo_id in expected.must_cite_events:
        try:
            required_ids |= resolved_ids([pseudo_id], events)
        except UnresolvedLabelError:
            unresolved.append(pseudo_id)

    if not required_ids:
        return 0.0, tuple(unresolved)

    cited_ids = {m.group("id") for m in CITATION.finditer(document_md)}
    return len(required_ids & cited_ids) / len(required_ids), tuple(unresolved)


# ---------------------------------------------------------------------------
# hard gates (docs/07-evaluation.md's CI-gate table)
# ---------------------------------------------------------------------------
#: Hallucinated-citation rate is a hard gate with target exactly zero (CLAUDE.md
#: invariant 3). Coverage and precision@1 are the other two fail-gates; decoy
#: resistance and cost are warn-gates (they degrade, they don't break CI).
GATE_HALLUCINATIONS_MAX = 0.0
GATE_COVERAGE_MIN = 0.95
GATE_PRECISION_AT_1_MIN = 0.70
WARN_DECOY_MIN = 0.90
WARN_COST_USD_MAX = 0.15


def check_gates(suite: SuiteResult) -> tuple[list[str], list[str]]:
    """Return `(failures, warnings)` for a whole-suite run, per docs/07.

    Failures are the three hard gates (hallucinations `== 0`, coverage
    `>= 0.95`, precision@1 `>= 0.70`) — any breach fails CI. Warnings are the
    two soft gates (decoy resistance, cost) — printed, not fatal. Each list is
    human-readable ("why it failed"), not just a number.
    """
    failures: list[str] = []
    warnings: list[str] = []

    if suite.hallucinated_citation_rate != GATE_HALLUCINATIONS_MAX:
        failures.append(
            f"hallucinated citations {suite.hallucinated_citation_rate:.3f} != 0.000"
        )
    if suite.citation_coverage < GATE_COVERAGE_MIN:
        failures.append(
            f"citation coverage {suite.citation_coverage:.3f} < {GATE_COVERAGE_MIN:.2f}"
        )
    if suite.precision_at_1 < GATE_PRECISION_AT_1_MIN:
        failures.append(
            f"precision@1 {suite.precision_at_1:.2f} < {GATE_PRECISION_AT_1_MIN:.2f}"
        )
    if suite.decoy_resistance < WARN_DECOY_MIN:
        warnings.append(
            f"decoy resistance {suite.decoy_resistance:.2f} < {WARN_DECOY_MIN:.2f}"
        )
    if suite.cost_usd is not None and suite.cost_usd > WARN_COST_USD_MAX:
        warnings.append(f"cost ${suite.cost_usd:.2f} > ${WARN_COST_USD_MAX:.2f}")
    return failures, warnings


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------
#: (input $/1M tokens, output $/1M tokens). Anthropic's published pricing as
#: of 2026-01 — the one place this module assumes a fact rather than reading
#: it, and the one place to update if pricing changes. `RunReport.token_usage`
#: (`agent/graph.py`'s `_add_tokens`) only tracks input+output combined per
#: node, not split — so cost here is a *blended average* of the two rates,
#: not an exact figure. Stated as an approximation, not hidden as one.
_PRICING_PER_MTOK: Final[dict[str, tuple[float, float]]] = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (15.00, 75.00),
}


def cost_per_run(token_usage: Mapping[str, int], model: str) -> float | None:
    """`None` for an unpriced model or the mock provider — "unknown" and
    "free" are different facts, and collapsing them would make a mock run
    indistinguishable from a real one costing nothing in a report."""
    pricing = _PRICING_PER_MTOK.get(model)
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    blended_rate = (input_rate + output_rate) / 2
    total_tokens = sum(token_usage.values())
    return total_tokens * blended_rate / 1_000_000


# ---------------------------------------------------------------------------
# case-level bundle
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CaseResult:
    """Everything one case's run scored to, for the runner's report/history
    output. `unresolved_labels` is non-empty exactly when a fixture's
    `expected.yaml` references an event the run didn't actually produce —
    always worth fixing, since it silently weakens whatever metric depended
    on that label."""

    case_id: str
    passed_validation: bool
    precision_at_1: bool
    recall_at_3: bool
    citation_coverage: float
    hallucinated_citation_rate: float
    decoy_resistance: bool
    abstention_correctness: bool | None
    must_cite_recall: float
    confidence: float
    band: str
    tokens_total: int
    cost_usd: float | None
    duration_seconds: float
    unresolved_labels: tuple[str, ...] = ()


def score_case(
    *,
    case_id: str,
    expected: ExpectedLabel,
    hypotheses: Sequence[Hypothesis],
    validation: ValidationReport | None,
    document_md: str,
    events: Sequence[Event],
    token_usage: Mapping[str, int],
    duration_seconds: float,
    model: str,
) -> CaseResult:
    """Score one finished run. Resolution failures (a fixture referencing an
    event the run never produced) are collected rather than raised — one
    broken label shouldn't hide every other metric for the same case."""
    unresolved: list[str] = []

    try:
        correct_ids = correct_event_ids(expected, events)
    except UnresolvedLabelError as exc:
        unresolved.append(str(exc))
        correct_ids = frozenset()

    cite_recall, cite_unresolved = must_cite_recall(document_md, expected, events)
    unresolved.extend(cite_unresolved)

    top = min(hypotheses, key=lambda h: h.rank) if hypotheses else None

    return CaseResult(
        case_id=case_id,
        passed_validation=validation.passed if validation else False,
        precision_at_1=precision_at_1(hypotheses, correct_ids),
        recall_at_3=recall_at_3(hypotheses, correct_ids),
        citation_coverage=validation.coverage if validation else 0.0,
        hallucinated_citation_rate=(
            validation.hallucination_rate if validation else 1.0
        ),
        decoy_resistance=decoy_resistance(hypotheses, expected),
        abstention_correctness=abstention_correctness(hypotheses, expected),
        must_cite_recall=cite_recall,
        confidence=top.confidence if top else 0.0,
        band=top.band if top else "none",
        tokens_total=sum(token_usage.values()),
        cost_usd=cost_per_run(token_usage, model),
        duration_seconds=duration_seconds,
        unresolved_labels=tuple(unresolved),
    )


# ---------------------------------------------------------------------------
# suite-level aggregation
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SuiteResult:
    """The whole run's numbers — the summary table docs/07 shows."""

    case_results: tuple[CaseResult, ...]
    precision_at_1: float
    recall_at_3: float
    citation_coverage: float
    hallucinated_citation_rate: float
    decoy_resistance: float
    abstention_correctness: float | None
    confidence_separation: float | None
    tokens_total: int
    cost_usd: float | None
    latency_p50: float
    latency_p95: float


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[index]


def confidence_separation(case_results: Sequence[CaseResult]) -> float | None:
    """Mean confidence(correct) minus mean confidence(incorrect). `None` when
    either group is empty — the number is meaningless without both to
    contrast, not `0.0` (which would misleadingly read as "no separation")."""
    correct = [c.confidence for c in case_results if c.precision_at_1]
    incorrect = [c.confidence for c in case_results if not c.precision_at_1]
    if not correct or not incorrect:
        return None
    return _mean(correct) - _mean(incorrect)


def aggregate(case_results: Sequence[CaseResult]) -> SuiteResult:
    abstention_scores = [
        c.abstention_correctness
        for c in case_results
        if c.abstention_correctness is not None
    ]
    costs = [c.cost_usd for c in case_results if c.cost_usd is not None]
    durations = [c.duration_seconds for c in case_results]

    return SuiteResult(
        case_results=tuple(case_results),
        precision_at_1=_mean([float(c.precision_at_1) for c in case_results]),
        recall_at_3=_mean([float(c.recall_at_3) for c in case_results]),
        citation_coverage=_mean([c.citation_coverage for c in case_results]),
        hallucinated_citation_rate=_mean(
            [c.hallucinated_citation_rate for c in case_results]
        ),
        decoy_resistance=_mean([float(c.decoy_resistance) for c in case_results]),
        abstention_correctness=(
            _mean([float(a) for a in abstention_scores]) if abstention_scores else None
        ),
        confidence_separation=confidence_separation(case_results),
        tokens_total=sum(c.tokens_total for c in case_results),
        cost_usd=sum(costs) if costs else None,
        latency_p50=_percentile(durations, 0.50),
        latency_p95=_percentile(durations, 0.95),
    )
