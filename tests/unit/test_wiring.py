"""The DAG's branch logic, the run report, and the CLI's incident loader.

The full pipeline needs Neo4j and lives in the integration suite. What is unit
tested here is the part that decides *whether to retry*, because that is where
invariant 8 either holds or does not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.cli import build_parser, load_incident_spec
from agent.config import ConfigError
from agent.graph import _after_validation, initial_state
from agent.observability.report import build_run_report, report_to_dict
from agent.state import (
    Complaint,
    ComplaintKind,
    Incident,
    PipelineState,
    SourceFailure,
    ValidationReport,
)

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)


def an_incident() -> Incident:
    return Incident(
        id="INC-0001",
        title="checkout 500s",
        start_time=START,
        end_time=START + timedelta(hours=1),
        severity="sev2",
    )


def a_report(*, passed: bool) -> ValidationReport:
    return ValidationReport(
        incident_id="INC-0001",
        factual_sentences=10,
        cited_sentences=10 if passed else 5,
        citations_total=10,
        citations_hallucinated=0 if passed else 1,
        coverage_threshold=0.95,
        complaints=()
        if passed
        else (
            Complaint(
                kind=ComplaintKind.HALLUCINATED_ID,
                detail="cited 9f2b which does not exist",
                sentence_index=3,
                cited_id="9f2b",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# the retry branch — invariant 8
# ---------------------------------------------------------------------------
def test_a_passing_document_publishes():
    decide = _after_validation(max_retries=2)
    state = PipelineState(validation=a_report(passed=True), retry_count=0)
    assert decide(state) == "publish"


def test_a_failing_document_retries_while_budget_remains():
    decide = _after_validation(max_retries=2)
    for used in (0, 1):
        state = PipelineState(validation=a_report(passed=False), retry_count=used)
        assert decide(state) == "retry"


def test_the_budget_is_a_hard_stop():
    """Never a third rewrite. A pipeline that quietly keeps trying is a
    pipeline with an unbounded model spend."""
    decide = _after_validation(max_retries=2)
    state = PipelineState(validation=a_report(passed=False), retry_count=2)
    assert decide(state) == "fail"


def test_zero_retries_fails_immediately():
    decide = _after_validation(max_retries=0)
    state = PipelineState(validation=a_report(passed=False), retry_count=0)
    assert decide(state) == "fail"


def test_a_passing_document_publishes_even_at_the_retry_limit():
    """A draft repaired on the last attempt still ships."""
    decide = _after_validation(max_retries=2)
    state = PipelineState(validation=a_report(passed=True), retry_count=2)
    assert decide(state) == "publish"


def test_missing_validation_never_publishes():
    """Absence of a verdict is not a pass."""
    decide = _after_validation(max_retries=0)
    assert decide(PipelineState(retry_count=0)) == "fail"


def test_initial_state_starts_clean():
    state = initial_state(an_incident())
    assert state["retry_count"] == 0
    assert state["validation"] is None
    assert state["events"] == []


# ---------------------------------------------------------------------------
# the run report
# ---------------------------------------------------------------------------
def test_run_report_records_what_happened():
    state = PipelineState(
        incident_id="INC-0001",
        events=[],
        candidates=[],
        hypotheses=[],
        retry_count=1,
        token_usage={"analyst": 100, "writer": 200},
        sources_used=["logs", "git"],
        sources_failed=[SourceFailure("alerts", "fixture not found")],
        validation=a_report(passed=True),
    )
    report = build_run_report(
        state, started_at=START, finished_at=START + timedelta(seconds=42)
    )
    assert report.succeeded
    assert report.duration_seconds == pytest.approx(42.0)
    assert report.sources_failed[0].source == "alerts"


def test_run_report_json_is_serializable_and_flat():
    """The eval harness parses this, so the shape is a contract."""
    state = PipelineState(
        incident_id="INC-0001",
        token_usage={"analyst": 100, "writer": 200},
        sources_used=["logs"],
        sources_failed=[SourceFailure("alerts", "boom")],
        validation=a_report(passed=False),
        retry_count=2,
    )
    report = build_run_report(
        state, started_at=START, finished_at=START + timedelta(seconds=5)
    )
    payload = report_to_dict(report)
    round_tripped = json.loads(json.dumps(payload))

    assert round_tripped["succeeded"] is False
    assert round_tripped["total_tokens"] == 300
    assert round_tripped["validation"]["citations_hallucinated"] == 1
    assert round_tripped["validation"]["complaints"][0]["kind"] == "hallucinated_id"


def test_a_failed_run_still_produces_a_report():
    """The failing report is the more useful of the two — it says why."""
    state = PipelineState(incident_id="INC-0001", validation=a_report(passed=False))
    report = build_run_report(state, started_at=START, finished_at=START)
    assert not report.succeeded
    assert report_to_dict(report)["validation"]["complaints"]


def test_report_without_validation_is_not_a_success():
    state = PipelineState(incident_id="INC-0001", validation=None)
    report = build_run_report(state, started_at=START, finished_at=START)
    assert not report.succeeded
    assert report_to_dict(report)["validation"] is None


# ---------------------------------------------------------------------------
# incident specs
# ---------------------------------------------------------------------------
def write_meta(tmp_path: Path, body: str) -> Path:
    (tmp_path / "meta.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def test_incident_spec_loads():
    spec = load_incident_spec(
        Path("evals/incidents/inc-0001-missing-env-var").resolve()
    )
    assert spec.incident.id == "INC-0001"
    assert spec.incident.start_time.tzinfo is not None
    assert spec.logs_path.name == "logs"
    assert spec.alerts_path.name == "alerts.json"


def test_missing_meta_fails_loudly(tmp_path: Path):
    with pytest.raises(ConfigError, match=r"no meta\.yaml"):
        load_incident_spec(tmp_path)


def test_incomplete_meta_names_what_is_missing(tmp_path: Path):
    directory = write_meta(tmp_path, "id: INC-0001\ntitle: something\n")
    with pytest.raises(ConfigError, match="missing"):
        load_incident_spec(directory)


def test_bad_timestamp_in_meta_fails_loudly(tmp_path: Path):
    directory = write_meta(
        tmp_path,
        "id: INC-0001\ntitle: t\nstart: last Tuesday\nend: 2026-03-12T15:00:00Z\n",
    )
    with pytest.raises(ConfigError, match="not an ISO timestamp"):
        load_incident_spec(directory)


def test_naive_timestamps_in_meta_are_treated_as_utc(tmp_path: Path):
    directory = write_meta(
        tmp_path,
        "id: INC-0001\ntitle: t\n"
        "start: 2026-03-12 14:00:00\nend: 2026-03-12 15:00:00\n",
    )
    spec = load_incident_spec(directory)
    assert spec.incident.start_time.tzinfo is not None


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------
def test_run_requires_an_incident():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run"])


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


@pytest.mark.parametrize("command", ["schema-init", "eval"])
def test_simple_commands_parse(command: str):
    assert build_parser().parse_args([command]).command == command


def test_validate_command_parses():
    args = build_parser().parse_args(
        ["validate", "--incident-id", "INC-0001", "--document", "out/x.md"]
    )
    assert args.incident_id == "INC-0001"
    assert args.document == Path("out/x.md")
