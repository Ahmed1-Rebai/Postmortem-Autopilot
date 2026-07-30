"""The whole DAG, end to end, against real Neo4j and the mock provider.

No API key and no network: this is the path CI runs, which is the entire point
of the mock existing (trap 2). It proves the nodes compose — that collect feeds
graph feeds link feeds analyze feeds write feeds validate — which no amount of
unit testing the pieces can show.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.cli import IncidentSpec, build_collectors
from agent.config import (
    ConfidenceConfig,
    Config,
    LLMConfig,
    Neo4jConfig,
    PipelineConfig,
    ServicesConfig,
    SourcesConfig,
    ValkeyConfig,
)
from agent.graph import PipelineDeps, build_pipeline, initial_state
from agent.linker import CandidateLinker, LinkerConfig
from agent.llm import MockProvider
from agent.memory import Neo4jMemory
from agent.normalize.services import ServiceCanonicalizer
from agent.observability.report import build_run_report, report_to_dict
from agent.state import Incident

pytestmark = pytest.mark.integration

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)


def a_config(tmp_path: Path, *, max_retries: int = 2) -> Config:
    return Config(
        llm=LLMConfig(
            provider="mock", analyst_model="mock", writer_model="mock", api_key=None
        ),
        neo4j=Neo4jConfig(uri="bolt://unused", user="neo4j", password="x"),
        valkey=ValkeyConfig(url="redis://unused"),
        pipeline=PipelineConfig(
            causal_window_minutes=15,
            citation_coverage_threshold=0.95,
            max_validation_retries=max_retries,
            max_events_per_run=5000,
        ),
        sources=SourcesConfig(
            log_source_path=tmp_path, git_repo_path=tmp_path, alerts_fixture=tmp_path
        ),
        confidence=ConfidenceConfig(
            weights={
                "change_path_overlap": 0.30,
                "log_signature_match": 0.25,
                "metric_correlation": 0.20,
                "temporal_proximity": 0.15,
                "human_confirmation": 0.10,
            },
            contradiction_per_event=0.15,
            contradiction_cap=0.40,
            band_likely=0.75,
            band_plausible=0.40,
        ),
        services=ServicesConfig(aliases={}, strip_suffixes=["-svc", "-service"]),
        output_dir=tmp_path / "out",
        prompts_dir=None,
        pushgateway_url=None,
    )


def a_fixture(tmp_path: Path) -> IncidentSpec:
    """A miniature incident on disk: logs, alerts, and no git repo."""
    logs = tmp_path / "logs"
    logs.mkdir()
    lines = []
    for i in range(40):
        stamp = (START + timedelta(seconds=i * 5)).isoformat().replace("+00:00", "Z")
        if i > 10:
            lines.append(
                f"{stamp} ERROR [checkout-svc] ConnectionError: pool exhausted ({i}/32)"
            )
        else:
            lines.append(f"{stamp} INFO [checkout-svc] request served")
    (logs / "app.log").write_text("\n".join(lines), encoding="utf-8")

    (tmp_path / "alerts.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "HighErrorRate",
                            "service": "checkout",
                            "severity": "page",
                        },
                        "annotations": {"summary": "error rate above 5%"},
                        "startsAt": (START + timedelta(minutes=4))
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    incident = Incident(
        id="INC-PIPE",
        title="checkout 500s",
        start_time=START,
        end_time=START + timedelta(hours=1),
        severity="sev2",
    )
    return IncidentSpec(
        incident=incident,
        directory=tmp_path,
        logs_path=logs,
        repo_path=tmp_path / "no-such-repo",
        alerts_path=tmp_path / "alerts.json",
    )


def deps_for(
    memory: Neo4jMemory, tmp_path: Path, provider: MockProvider, **kw: object
) -> tuple[PipelineDeps, IncidentSpec]:
    config = a_config(tmp_path, **kw)  # type: ignore[arg-type]
    spec = a_fixture(tmp_path)
    canonicalizer = ServiceCanonicalizer.from_config({}, ["-svc", "-service"])
    return (
        PipelineDeps(
            config=config,
            memory=memory,
            provider=provider,
            collectors=build_collectors(spec),
            canonicalizer=canonicalizer,
            linker=CandidateLinker(LinkerConfig(window_minutes=15), canonicalizer),
        ),
        spec,
    )


def test_the_whole_pipeline_runs_and_publishes(memory: Neo4jMemory, tmp_path: Path):
    deps, spec = deps_for(memory, tmp_path, MockProvider())
    final = build_pipeline(deps).invoke(initial_state(spec.incident))

    assert final["graph_ready"]
    assert final["events"], "collection produced nothing"
    assert final["hypotheses"], "the analyst produced no hypotheses"
    assert final["document_md"], "nothing was published"
    assert final["validation"] is not None


def test_every_citation_in_the_published_document_resolves(
    memory: Neo4jMemory, tmp_path: Path
):
    """The project's central claim, exercised through the whole pipeline."""
    deps, spec = deps_for(memory, tmp_path, MockProvider())
    final = build_pipeline(deps).invoke(initial_state(spec.incident))

    report = final["validation"]
    assert report is not None
    assert report.citations_hallucinated == 0
    assert report.timestamp_mismatches == 0


def test_a_failed_collector_degrades_rather_than_aborting(
    memory: Neo4jMemory, tmp_path: Path
):
    """The fixture has no git repo, so that collector fails. The run continues
    with fewer sources and records the gap."""
    deps, spec = deps_for(memory, tmp_path, MockProvider())
    final = build_pipeline(deps).invoke(initial_state(spec.incident))

    assert "git" not in final["sources_used"]
    assert [f.source for f in final["sources_failed"]] == ["git"]
    assert final["events"], "the run should still have produced events"


def test_the_document_contains_the_code_rendered_timeline(
    memory: Neo4jMemory, tmp_path: Path
):
    deps, spec = deps_for(memory, tmp_path, MockProvider())
    final = build_pipeline(deps).invoke(initial_state(spec.incident))

    document = final["document_md"]
    assert "## Timeline" in document
    for event in final["events"]:
        assert event.id in document, "every event should appear in the timeline"


def test_running_the_same_incident_twice_is_idempotent(
    memory: Neo4jMemory, tmp_path: Path
):
    """Invariant 5 at the pipeline level: a rerun must not duplicate evidence,
    or every confidence score silently inflates."""
    deps, spec = deps_for(memory, tmp_path, MockProvider())
    pipeline = build_pipeline(deps)

    pipeline.invoke(initial_state(spec.incident))
    first = memory.count_events(spec.incident.id)
    pipeline.invoke(initial_state(spec.incident))
    second = memory.count_events(spec.incident.id)

    assert first == second


def test_an_unrepairable_draft_fails_loudly_and_is_not_published(
    memory: Neo4jMemory, tmp_path: Path
):
    """Invariant 8. The model is scripted to keep citing a fabricated ID; the
    pipeline must exhaust its retries and refuse to pass it."""
    fabricating = MockProvider(
        scripted=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "statement": "something happened",
                            "supporting_event_ids": [],
                            "contradicting_event_ids": [],
                        }
                    ]
                }
            ),
            *[
                "## Summary\n\nThe service failed catastrophically "
                "[src:9f2b000000000000].\n"
            ]
            * 3,
        ]
    )
    deps, spec = deps_for(memory, tmp_path, fabricating, max_retries=2)
    final = build_pipeline(deps).invoke(initial_state(spec.incident))

    report = final["validation"]
    assert report is not None
    assert not report.passed
    assert report.citations_hallucinated >= 1
    assert final["retry_count"] == 2, "the full budget should have been used"


def test_the_retry_budget_is_not_exceeded(memory: Neo4jMemory, tmp_path: Path):
    """A miscounted retry would become an unbounded model spend."""
    fabricating = MockProvider(
        scripted=[
            json.dumps(
                {"hypotheses": [{"statement": "x", "supporting_event_ids": []}]}
            ),
            *["## Summary\n\nIt broke badly [src:9f2b000000000000].\n"] * 5,
        ]
    )
    deps, spec = deps_for(memory, tmp_path, fabricating, max_retries=1)
    final = build_pipeline(deps).invoke(initial_state(spec.incident))

    assert final["retry_count"] == 1
    # analyst once, writer twice (initial + one repair)
    assert len(fabricating.calls) == 3


def test_the_run_report_reflects_the_run(memory: Neo4jMemory, tmp_path: Path):
    deps, spec = deps_for(memory, tmp_path, MockProvider())
    final = build_pipeline(deps).invoke(initial_state(spec.incident))

    report = build_run_report(
        final, started_at=START, finished_at=START + timedelta(seconds=3)
    )
    payload = report_to_dict(report)
    assert payload["event_count"] == len(final["events"])
    assert payload["sources_failed"][0]["source"] == "git"
    assert payload["total_tokens"] > 0
