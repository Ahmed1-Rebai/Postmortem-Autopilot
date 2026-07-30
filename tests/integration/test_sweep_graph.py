"""`find_incidents_needing_postmortem` and `build_recovery_pipeline`, against
real Neo4j — the two pieces the nightly sweep is built from.

`build_pipeline(...).invoke(...)`, called directly rather than through
`cli.cmd_run`, already leaves exactly the graph state sweep exists to catch:
`_publish_node` never calls `persist_hypotheses` (only `cmd_run`'s tail does,
on success), so an `Incident` with `Event`s and no `Hypothesis` is what every
other pipeline integration test's graph already looks like after a run —
this file is what points that same state at the code that's supposed to
notice and fix it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.cli import IncidentSpec, build_collectors, load_incident_spec
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
from agent.graph import (
    PipelineDeps,
    build_pipeline,
    build_recovery_pipeline,
    initial_state,
)
from agent.linker import CandidateLinker, LinkerConfig
from agent.llm import MockProvider
from agent.memory import Neo4jMemory
from agent.normalize.services import ServiceCanonicalizer
from agent.state import Incident

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "incidents"


def a_config() -> Config:
    return Config(
        llm=LLMConfig(
            provider="mock", analyst_model="mock", writer_model="mock", api_key=None
        ),
        neo4j=Neo4jConfig(uri="bolt://unused", user="neo4j", password="x"),
        valkey=ValkeyConfig(url="redis://unused"),
        pipeline=PipelineConfig(
            causal_window_minutes=15,
            citation_coverage_threshold=0.95,
            max_validation_retries=2,
            max_events_per_run=5000,
        ),
        sources=SourcesConfig(
            log_source_path=FIXTURES, git_repo_path=FIXTURES, alerts_fixture=FIXTURES
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
        output_dir=FIXTURES,
        prompts_dir=None,
        pushgateway_url=None,
    )


def deps_for(memory: Neo4jMemory) -> tuple[PipelineDeps, IncidentSpec]:
    config = a_config()
    spec = load_incident_spec(FIXTURES / "inc-0001-missing-env-var")
    canonicalizer = ServiceCanonicalizer.from_config(
        config.services.aliases, config.services.strip_suffixes
    )
    deps = PipelineDeps(
        config=config,
        memory=memory,
        provider=MockProvider(),
        collectors=build_collectors(spec),
        canonicalizer=canonicalizer,
        linker=CandidateLinker(LinkerConfig(window_minutes=15), canonicalizer),
    )
    return deps, spec


def seed_unfinished_run(memory: Neo4jMemory) -> Incident:
    """Run the real pipeline once, directly (bypassing `cmd_run`), so the
    graph ends up with an `Incident` and its `Event`s but no `Hypothesis` —
    exactly what a crashed-after-collection Job leaves behind."""
    deps, spec = deps_for(memory)
    build_pipeline(deps).invoke(initial_state(spec.incident))
    return spec.incident


def test_finds_an_incident_with_events_but_no_hypothesis(memory: Neo4jMemory):
    incident = seed_unfinished_run(memory)

    found = memory.find_incidents_needing_postmortem(
        since=incident.start_time - timedelta(days=1)
    )

    assert [i.id for i in found] == [incident.id]


def test_does_not_find_incidents_with_hypotheses_already_persisted(
    memory: Neo4jMemory,
):
    """Once a run reaches `persist_hypotheses` (what `cmd_run` calls on
    success), the incident is no longer "needing a postmortem" — the sweep
    must not re-process work that already finished."""
    deps, spec = deps_for(memory)
    final = build_pipeline(deps).invoke(initial_state(spec.incident))
    memory.persist_hypotheses(
        spec.incident.id, list(final["hypotheses"]), now=datetime.now(UTC)
    )

    found = memory.find_incidents_needing_postmortem(
        since=spec.incident.start_time - timedelta(days=1)
    )

    assert found == []


def test_does_not_find_incidents_outside_the_since_window(memory: Neo4jMemory):
    incident = seed_unfinished_run(memory)

    found = memory.find_incidents_needing_postmortem(
        since=incident.end_time + timedelta(days=1)
    )

    assert found == []


def test_recovery_pipeline_resumes_from_graph_state_and_publishes(
    memory: Neo4jMemory,
):
    """The core claim: no directory, no collectors, no fresh evidence — just
    what's already in the graph — and the recovery pipeline still produces a
    fully-cited document."""
    incident = seed_unfinished_run(memory)
    events = memory.get_timeline(incident.id)
    assert events, "the seeded run should have written events"

    config = a_config()
    canonicalizer = ServiceCanonicalizer.from_config(
        config.services.aliases, config.services.strip_suffixes
    )
    deps = PipelineDeps(
        config=config,
        memory=memory,
        provider=MockProvider(),
        collectors=(),
        canonicalizer=canonicalizer,
        linker=CandidateLinker(LinkerConfig(window_minutes=15), canonicalizer),
    )

    sources_used = sorted({e.source for e in events})
    final = build_recovery_pipeline(deps).invoke(
        initial_state(incident, events=events, sources_used=sources_used)
    )

    assert final["hypotheses"], "the analyst produced no hypotheses"
    assert final["document_md"], "nothing was published"
    report = final["validation"]
    assert report is not None
    assert report.citations_hallucinated == 0

    memory.persist_hypotheses(
        incident.id, list(final["hypotheses"]), now=datetime.now(UTC)
    )
    found = memory.find_incidents_needing_postmortem(
        since=incident.start_time - timedelta(days=1)
    )
    assert found == [], "sweep should consider the incident recovered"


def test_recovery_pipeline_does_not_duplicate_events(memory: Neo4jMemory):
    """Invariant 5 at the recovery pipeline's level: re-running `build_graph`
    on events already in the graph must not inflate the count."""
    incident = seed_unfinished_run(memory)
    before = memory.count_events(incident.id)
    events = memory.get_timeline(incident.id)

    config = a_config()
    canonicalizer = ServiceCanonicalizer.from_config(
        config.services.aliases, config.services.strip_suffixes
    )
    deps = PipelineDeps(
        config=config,
        memory=memory,
        provider=MockProvider(),
        collectors=(),
        canonicalizer=canonicalizer,
        linker=CandidateLinker(LinkerConfig(window_minutes=15), canonicalizer),
    )
    build_recovery_pipeline(deps).invoke(
        initial_state(
            incident, events=events, sources_used=sorted({e.source for e in events})
        )
    )

    assert memory.count_events(incident.id) == before
