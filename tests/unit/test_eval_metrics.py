"""`evals/metrics.py` — expected-value tables for each docs/07 metric."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent.state import Event, EventType, Hypothesis, ValidationReport
from evals.metrics import (
    CaseResult,
    ExpectedLabel,
    abstention_correctness,
    aggregate,
    confidence_separation,
    correct_event_ids,
    cost_per_run,
    decoy_resistance,
    must_cite_recall,
    precision_at_1,
    recall_at_3,
    score_case,
)

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)


def a_commit(summary: str, event_id: str) -> Event:
    return Event(
        id=event_id,
        type=EventType.COMMIT,
        timestamp=START,
        source="git",
        summary=summary,
    )


def an_expected(**overrides: object) -> ExpectedLabel:
    defaults: dict[str, object] = {
        "case_id": "inc-test",
        "root_cause_event_id": "commit:remove unused DB_POOL_MAX from settings",
        "root_cause_summary": "pool exhausted",
        "acceptable_alternates": (),
        "must_cite_events": (),
        "must_not_conclude": (),
        "min_confidence": 0.70,
        "is_abstention_case": False,
    }
    defaults.update(overrides)
    return ExpectedLabel(**defaults)  # type: ignore[arg-type]


def a_hypothesis(
    rank: int = 1,
    supporting: tuple[str, ...] = (),
    confidence: float = 0.8,
    band: str = "likely",
    statement: str = "the deploy caused it",
) -> Hypothesis:
    return Hypothesis(
        id=f"hyp-{rank}",
        rank=rank,
        statement=statement,
        supporting_event_ids=supporting,
        contradicting_event_ids=(),
        confidence=confidence,
        band=band,  # type: ignore[arg-type]
    )


def a_validation(
    factual: int = 4, cited: int = 4, hallucinated: int = 0
) -> ValidationReport:
    return ValidationReport(
        incident_id="inc-test",
        factual_sentences=factual,
        cited_sentences=cited,
        citations_total=cited,
        citations_hallucinated=hallucinated,
        coverage_threshold=0.95,
    )


# ---------------------------------------------------------------------------
# correct_event_ids / precision@1 / recall@3
# ---------------------------------------------------------------------------
def test_correct_event_ids_resolves_root_cause_and_alternates():
    commit = a_commit("remove unused DB_POOL_MAX from settings", "real-id")
    expected = an_expected(acceptable_alternates=())
    assert correct_event_ids(expected, [commit]) == frozenset({"real-id"})


def test_precision_at_1_true_when_top_hypothesis_cites_the_correct_event():
    hyp = a_hypothesis(rank=1, supporting=("real-id",))
    assert precision_at_1([hyp], frozenset({"real-id"})) is True


def test_precision_at_1_false_when_top_hypothesis_cites_something_else():
    hyp = a_hypothesis(rank=1, supporting=("other-id",))
    assert precision_at_1([hyp], frozenset({"real-id"})) is False


def test_precision_at_1_ignores_a_correct_citation_outside_the_top_rank():
    top = a_hypothesis(rank=1, supporting=("other-id",))
    second = a_hypothesis(rank=2, supporting=("real-id",))
    assert precision_at_1([top, second], frozenset({"real-id"})) is False


def test_precision_at_1_false_with_no_hypotheses():
    assert precision_at_1([], frozenset({"real-id"})) is False


def test_precision_at_1_uses_rank_not_list_order():
    """The top hypothesis is whichever has rank 1, not whichever is first
    in the list the caller happens to pass."""
    first_in_list = a_hypothesis(rank=2, supporting=("other-id",))
    actually_top = a_hypothesis(rank=1, supporting=("real-id",))
    assert precision_at_1([first_in_list, actually_top], frozenset({"real-id"})) is True


def test_recall_at_3_true_when_correct_event_is_third():
    hyps = [
        a_hypothesis(rank=1, supporting=("wrong-1",)),
        a_hypothesis(rank=2, supporting=("wrong-2",)),
        a_hypothesis(rank=3, supporting=("real-id",)),
    ]
    assert recall_at_3(hyps, frozenset({"real-id"})) is True


def test_recall_at_3_false_when_correct_event_is_fourth():
    hyps = [
        a_hypothesis(rank=1, supporting=("wrong-1",)),
        a_hypothesis(rank=2, supporting=("wrong-2",)),
        a_hypothesis(rank=3, supporting=("wrong-3",)),
        a_hypothesis(rank=4, supporting=("real-id",)),
    ]
    assert recall_at_3(hyps, frozenset({"real-id"})) is False


# ---------------------------------------------------------------------------
# decoy resistance
# ---------------------------------------------------------------------------
def test_decoy_resistance_true_when_no_decoy_mentioned():
    hyp = a_hypothesis(statement="the deploy removed a pool setting")
    expected = an_expected(must_not_conclude=("payment gateway latency",))
    assert decoy_resistance([hyp], expected) is True


def test_decoy_resistance_false_when_top_hypothesis_names_the_decoy():
    hyp = a_hypothesis(statement="likely caused by payment gateway latency")
    expected = an_expected(must_not_conclude=("payment gateway latency",))
    assert decoy_resistance([hyp], expected) is False


def test_decoy_resistance_is_case_insensitive():
    hyp = a_hypothesis(statement="Payment Gateway Latency is the cause")
    expected = an_expected(must_not_conclude=("payment gateway latency",))
    assert decoy_resistance([hyp], expected) is False


def test_decoy_resistance_true_when_no_decoys_defined():
    hyp = a_hypothesis(statement="anything at all")
    assert decoy_resistance([hyp], an_expected(must_not_conclude=())) is True


# ---------------------------------------------------------------------------
# abstention correctness
# ---------------------------------------------------------------------------
def test_abstention_correctness_none_for_a_non_abstention_case():
    hyp = a_hypothesis(band="likely")
    expected = an_expected(is_abstention_case=False)
    assert abstention_correctness([hyp], expected) is None


def test_abstention_correctness_true_when_tentative_as_required():
    hyp = a_hypothesis(band="tentative")
    expected = an_expected(is_abstention_case=True)
    assert abstention_correctness([hyp], expected) is True


def test_abstention_correctness_false_when_confidently_wrong():
    hyp = a_hypothesis(band="likely")
    expected = an_expected(is_abstention_case=True)
    assert abstention_correctness([hyp], expected) is False


# ---------------------------------------------------------------------------
# must-cite recall
# ---------------------------------------------------------------------------
def test_must_cite_recall_full_when_all_cited():
    event = a_commit("remove unused DB_POOL_MAX from settings", "real-id")
    expected = an_expected(
        must_cite_events=("commit:remove unused DB_POOL_MAX from settings",)
    )
    doc = "The pool broke [src:real-id]."
    fraction, unresolved = must_cite_recall(doc, expected, [event])
    assert fraction == 1.0
    assert unresolved == ()


def test_must_cite_recall_partial_when_only_some_cited():
    commit = a_commit("remove unused DB_POOL_MAX from settings", "commit-id")
    alert = Event(
        id="alert-id",
        type=EventType.ALERT_FIRED,
        timestamp=START,
        source="alerts",
        summary="HighErrorRate",
        attributes={"rule_name": "HighErrorRate"},
    )
    expected = an_expected(
        must_cite_events=(
            "commit:remove unused DB_POOL_MAX from settings",
            "alert:HighErrorRate",
        )
    )
    doc = "The pool broke [src:commit-id]."  # alert never cited
    fraction, unresolved = must_cite_recall(doc, expected, [commit, alert])
    assert fraction == 0.5
    assert unresolved == ()


def test_must_cite_recall_reports_unresolved_labels_without_crashing():
    expected = an_expected(must_cite_events=("alert:NoSuchAlert",))
    fraction, unresolved = must_cite_recall("no citations here", expected, [])
    assert fraction == 0.0
    assert unresolved == ("alert:NoSuchAlert",)


def test_must_cite_recall_vacuous_true_when_nothing_required():
    fraction, unresolved = must_cite_recall("anything", an_expected(), [])
    assert fraction == 1.0
    assert unresolved == ()


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------
def test_cost_per_run_known_model():
    # blended rate for sonnet-5: (3 + 15) / 2 = 9 $/1M tokens
    cost = cost_per_run({"analyst": 500_000, "writer": 500_000}, "claude-sonnet-5")
    assert cost == 9.0


def test_cost_per_run_none_for_mock():
    assert cost_per_run({"analyst": 1000}, "mock") is None


def test_cost_per_run_none_for_unrecognized_model():
    assert cost_per_run({"analyst": 1000}, "some-future-model") is None


# ---------------------------------------------------------------------------
# score_case / aggregate / confidence_separation
# ---------------------------------------------------------------------------
def test_score_case_end_to_end():
    commit = a_commit("remove unused DB_POOL_MAX from settings", "real-id")
    expected = an_expected(
        must_cite_events=("commit:remove unused DB_POOL_MAX from settings",)
    )
    hyp = a_hypothesis(rank=1, supporting=("real-id",), confidence=0.9, band="likely")
    validation = a_validation()

    result = score_case(
        case_id="inc-test",
        expected=expected,
        hypotheses=[hyp],
        validation=validation,
        document_md="The pool broke [src:real-id].",
        events=[commit],
        token_usage={"analyst": 100, "writer": 100},
        duration_seconds=12.5,
        model="mock",
    )

    assert result.case_id == "inc-test"
    assert result.precision_at_1 is True
    assert result.recall_at_3 is True
    assert result.citation_coverage == 1.0
    assert result.hallucinated_citation_rate == 0.0
    assert result.must_cite_recall == 1.0
    assert result.confidence == 0.9
    assert result.band == "likely"
    assert result.tokens_total == 200
    assert result.cost_usd is None  # mock is unpriced
    assert result.unresolved_labels == ()


def test_score_case_surfaces_an_unresolved_label_without_crashing():
    expected = an_expected(root_cause_event_id="commit:this commit does not exist")
    hyp = a_hypothesis(rank=1, supporting=())
    result = score_case(
        case_id="inc-test",
        expected=expected,
        hypotheses=[hyp],
        validation=a_validation(),
        document_md="",
        events=[],
        token_usage={},
        duration_seconds=1.0,
        model="mock",
    )
    assert result.precision_at_1 is False
    assert len(result.unresolved_labels) == 1
    assert "this commit does not exist" in result.unresolved_labels[0]


def test_score_case_with_no_validation_report_fails_safely():
    result = score_case(
        case_id="inc-test",
        expected=an_expected(),
        hypotheses=[],
        validation=None,
        document_md="",
        events=[],
        token_usage={},
        duration_seconds=1.0,
        model="mock",
    )
    assert result.passed_validation is False
    assert result.hallucinated_citation_rate == 1.0
    assert result.band == "none"


def _a_case_result(**overrides: object) -> CaseResult:
    defaults: dict[str, object] = {
        "case_id": "inc-x",
        "passed_validation": True,
        "precision_at_1": True,
        "recall_at_3": True,
        "citation_coverage": 1.0,
        "hallucinated_citation_rate": 0.0,
        "decoy_resistance": True,
        "abstention_correctness": None,
        "must_cite_recall": 1.0,
        "confidence": 0.8,
        "band": "likely",
        "tokens_total": 100,
        "cost_usd": 0.01,
        "duration_seconds": 10.0,
        "unresolved_labels": (),
    }
    defaults.update(overrides)
    return CaseResult(**defaults)  # type: ignore[arg-type]


def test_confidence_separation_needs_both_groups():
    correct = _a_case_result(precision_at_1=True, confidence=0.9)
    incorrect = _a_case_result(precision_at_1=False, confidence=0.3)
    assert confidence_separation([correct, incorrect]) == pytest.approx(0.6)


def test_confidence_separation_none_when_all_correct():
    all_correct = [_a_case_result(precision_at_1=True) for _ in range(3)]
    assert confidence_separation(all_correct) is None


def test_aggregate_averages_the_right_things():
    results = [
        _a_case_result(case_id="a", precision_at_1=True, duration_seconds=10.0),
        _a_case_result(case_id="b", precision_at_1=False, duration_seconds=20.0),
    ]
    suite = aggregate(results)
    assert suite.precision_at_1 == 0.5
    assert suite.latency_p50 in (10.0, 20.0)  # small-n percentile, either is valid
    assert suite.tokens_total == 200


def test_aggregate_abstention_only_averages_applicable_cases():
    results = [
        _a_case_result(case_id="a", abstention_correctness=None),
        _a_case_result(case_id="b", abstention_correctness=True),
        _a_case_result(case_id="c", abstention_correctness=False),
    ]
    suite = aggregate(results)
    assert suite.abstention_correctness == 0.5


def test_aggregate_abstention_none_when_no_case_is_applicable():
    results = [_a_case_result(abstention_correctness=None)]
    assert aggregate(results).abstention_correctness is None
