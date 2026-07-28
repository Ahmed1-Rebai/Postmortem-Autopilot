"""The linker against the real graph.

Unit tests prove the heuristics fire correctly; this proves the edges survive
the round trip — that what the linker decided is what the Analyst will actually
be shown.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.linker import CandidateLinker, LinkerConfig
from agent.memory import Neo4jMemory
from agent.normalize.services import ServiceCanonicalizer
from agent.state import Event, EventType, Incident

pytestmark = pytest.mark.integration

START = datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)


def build_events() -> list[Event]:
    """Golden-incident-0001 in miniature, with a decoy."""
    return [
        Event(
            id="commit_cause",
            type=EventType.COMMIT,
            timestamp=START,
            source="git",
            summary="remove DB_POOL_MAX from settings",
            service="checkout",
            attributes={
                "files_changed": ("checkout/config/database.py", "checkout/app/pool.py")
            },
        ),
        Event(
            id="commit_decoy",
            type=EventType.COMMIT,
            timestamp=START + timedelta(seconds=20),
            source="git",
            summary="tweak inventory pagination",
            service="inventory",
            attributes={"files_changed": ("inventory/views.py",)},
        ),
        Event(
            id="log_symptom",
            type=EventType.LOG_ERROR,
            timestamp=START + timedelta(seconds=60),
            source="logs",
            summary="ConnectionError: pool exhausted (32/32)",
            service="checkout",
            signature="connectionerror: pool exhausted (<num>/<num>)",
            count=151,
        ),
        Event(
            id="alert_symptom",
            type=EventType.ALERT_FIRED,
            timestamp=START + timedelta(minutes=2),
            source="alertmanager",
            summary="HighErrorRate: error rate above 5%",
            service="checkout",
            signature="higherrorrate: error rate above <num>%",
        ),
    ]


@pytest.fixture
def linker() -> CandidateLinker:
    canonicalizer = ServiceCanonicalizer.from_config({}, ["-svc", "-service"])
    return CandidateLinker(LinkerConfig(window_minutes=15), canonicalizer)


def test_candidate_edges_survive_the_round_trip(
    memory: Neo4jMemory, linker: CandidateLinker
):
    incident = Incident(
        id="INC-0001",
        title="checkout 500s",
        start_time=START,
        end_time=START + timedelta(hours=1),
    )
    memory.upsert_incident(incident)
    events = build_events()
    memory.write_events(incident.id, events)

    links = linker.link(events)
    for link in links:
        memory.link_candidate(
            link.cause_id, link.effect_id, list(link.heuristics), link.delta_seconds
        )

    chains = memory.get_candidate_chains(incident.id)
    assert len(chains) == len(links)

    by_pair = {(chain.cause_id, chain.effect_id): chain for chain in chains}
    strong = by_pair[("commit_cause", "log_symptom")]
    assert set(strong.heuristics) == {
        "temporal_proximity",
        "service_overlap",
        "change_path_overlap",
    }
    assert strong.delta_seconds == 60

    weak = by_pair[("commit_decoy", "log_symptom")]
    assert set(weak.heuristics) == {"temporal_proximity"}


def test_the_causal_commit_ranks_above_the_decoy(
    memory: Neo4jMemory, linker: CandidateLinker
):
    """Ranking is done by the graph query, so it has to hold end to end — this
    is the discrimination that golden incident 0004 exists to measure."""
    incident = Incident(
        id="INC-0004",
        title="two deploys",
        start_time=START,
        end_time=START + timedelta(hours=1),
    )
    memory.upsert_incident(incident)
    events = build_events()
    memory.write_events(incident.id, events)
    for link in linker.link(events):
        memory.link_candidate(
            link.cause_id, link.effect_id, list(link.heuristics), link.delta_seconds
        )

    chains = memory.get_candidate_chains(incident.id)
    assert chains[0].cause_id == "commit_cause"
    assert chains[0].heuristic_count == 3


def test_relinking_the_same_candidates_is_idempotent(
    memory: Neo4jMemory, linker: CandidateLinker
):
    incident = Incident(
        id="INC-0001",
        title="checkout 500s",
        start_time=START,
        end_time=START + timedelta(hours=1),
    )
    memory.upsert_incident(incident)
    events = build_events()
    memory.write_events(incident.id, events)
    links = linker.link(events)

    for _ in range(2):
        for link in links:
            memory.link_candidate(
                link.cause_id,
                link.effect_id,
                list(link.heuristics),
                link.delta_seconds,
            )

    assert len(memory.get_candidate_chains(incident.id)) == len(links)


def test_fingerprint_is_built_from_the_linked_edges(
    memory: Neo4jMemory, linker: CandidateLinker
):
    """Recurrence depends on the linker's output, so the two must agree."""
    incident = Incident(
        id="INC-0001",
        title="checkout 500s",
        start_time=START,
        end_time=START + timedelta(hours=1),
    )
    memory.upsert_incident(incident)
    events = build_events()
    memory.write_events(incident.id, events)
    for link in linker.link(events):
        memory.link_candidate(
            link.cause_id, link.effect_id, list(link.heuristics), link.delta_seconds
        )

    fingerprint = memory.compute_fingerprint(incident.id)
    assert (
        "commit|log_error|checkout|connectionerror: pool exhausted (<num>/<num>)"
        in (fingerprint)
    )
