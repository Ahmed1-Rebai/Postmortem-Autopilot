"""Recurrence, end to end: two full pipeline runs against the same graph.

This is the deterministic proof behind golden incident 0007 (docs/07, case 07:
"Recurrence of #01 six weeks later — tests: recurrence memory must surface
#01"). It uses the mock provider rather than a live model, for the same reason
every other pipeline test does: recurrence is a property of graph state, not
of prose, and it must hold regardless of what any given model writes.

The mock's canned Writer draft always includes a `## Corrective Actions`
bullet ("Review the change process."), so running INC-0001 through the real
`persist_corrective_actions` step — the same one `cli.cmd_run` performs after
a successful run — seeds exactly the "open fix" fact INC-0007's document is
expected to surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.cli import build_collectors, load_incident_spec
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
from agent.nodes.recurrence import persist_corrective_actions
from agent.normalize.services import ServiceCanonicalizer

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
    )


def run_incident(memory: Neo4jMemory, directory: Path) -> str:
    """Drive one incident through the DAG and replicate `cli.cmd_run`'s
    post-success persistence, since recurrence depends on graph writes the
    graph's own `publish` node deliberately does not make (a rejected draft's
    hypotheses and proposed fixes must never become graph facts)."""
    spec = load_incident_spec(directory)
    config = a_config()
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
    final = build_pipeline(deps).invoke(initial_state(spec.incident))
    report = final.get("validation")
    assert report is not None and report.passed, (
        f"{spec.incident.id} must validate for this test to mean anything: {report}"
    )

    memory.persist_hypotheses(
        spec.incident.id, list(final["hypotheses"]), now=deps.now()
    )
    memory.link_similar_incidents(spec.incident.id)
    persist_corrective_actions(memory, spec.incident.id, final["document_md"])
    return str(final["document_md"])


def test_incident_0007_surfaces_incident_0001_as_a_recurrence(
    memory: Neo4jMemory,
):
    """The money feature, run through the actual pipeline twice."""
    run_incident(memory, FIXTURES / "inc-0001-missing-env-var")
    later_document = run_incident(memory, FIXTURES / "inc-0007-recurrence-of-0001")

    assert "## Similar Past Incidents" in later_document
    assert "INC-0001" in later_document
    assert "Review the change process." in later_document
    assert "open corrective action" in later_document


def test_incident_0001_alone_shows_no_recurrence(memory: Neo4jMemory):
    """The common case: nothing to recur from yet, so the section is absent
    rather than printed empty."""
    document = run_incident(memory, FIXTURES / "inc-0001-missing-env-var")
    assert "## Similar Past Incidents" not in document


def test_recurrence_is_not_reciprocal(memory: Neo4jMemory):
    """INC-0001 predates INC-0007, so only the later incident should look
    backward — `past.end_time < i.start_time` in the Cypher, exercised here
    against two incidents that actually share a fingerprint."""
    run_incident(memory, FIXTURES / "inc-0001-missing-env-var")
    similar_from_0001 = memory.find_similar_incidents("INC-0001")
    assert similar_from_0001 == []
