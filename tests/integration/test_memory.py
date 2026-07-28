"""`memory.py` against a real Neo4j.

The headline test is `test_writing_the_same_events_twice_creates_no_duplicates`.
Idempotency is load-bearing: a duplicated event inflates corroboration counts,
which silently corrupts every confidence score downstream, and nothing later in
the pipeline would notice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.memory import GraphStateError, Neo4jMemory
from agent.state import (
    Event,
    EventType,
    Hypothesis,
    Incident,
    make_event_id,
    make_hypothesis_id,
)

pytestmark = pytest.mark.integration

START = datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)


def an_incident(incident_id: str = "INC-0001", **overrides: object) -> Incident:
    kwargs: dict[str, object] = {
        "id": incident_id,
        "title": "checkout 500s after deploy",
        "start_time": START,
        "end_time": START + timedelta(hours=1),
        "severity": "sev2",
        "status": "closed",
    }
    kwargs.update(overrides)
    return Incident(**kwargs)  # type: ignore[arg-type]


def fifty_events() -> list[Event]:
    """A realistic mix: log errors, one deploy, one alert, one commit."""
    events: list[Event] = []
    for index in range(47):
        events.append(
            Event(
                id=make_event_id("loki", f"line-{index}"),
                type=EventType.LOG_ERROR,
                timestamp=START + timedelta(seconds=index * 10),
                source="loki",
                summary=f"ConnectionError: pool exhausted ({index}/32)",
                service="checkout",
                signature="connectionerror_pool_exhausted",
                count=index + 1,
                attributes={"level": "ERROR"},
            )
        )
    events.append(
        Event(
            id=make_event_id("github", "a3f9c1e2"),
            type=EventType.COMMIT,
            timestamp=START + timedelta(seconds=5),
            source="github",
            summary="remove DB_POOL_MAX from settings",
            service="checkout",
            attributes={
                "sha": "a3f9c1e2",
                "author": "someone",
                "files_changed": ("config/database.py", "app/pool.py"),
            },
        )
    )
    events.append(
        Event(
            id=make_event_id("argo", "deploy-991"),
            type=EventType.DEPLOY,
            timestamp=START + timedelta(seconds=40),
            source="argo",
            summary="checkout v2.3.1 rolled out",
            service="checkout",
            attributes={"commit_sha": "a3f9c1e2", "version": "v2.3.1"},
        )
    )
    events.append(
        Event(
            id=make_event_id("alertmanager", "HighErrorRate:1710252131"),
            type=EventType.ALERT_FIRED,
            timestamp=START + timedelta(minutes=2),
            source="alertmanager",
            summary="HighErrorRate{service=checkout} firing",
            service="checkout",
            attributes={"rule_name": "HighErrorRate", "severity": "page"},
        )
    )
    assert len(events) == 50
    return events


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def test_ensure_schema_is_idempotent(memory: Neo4jMemory):
    """It runs on every startup, so running it twice must be a non-event."""
    memory.ensure_schema()
    memory.ensure_schema()


def test_event_id_uniqueness_constraint_exists(
    memory: Neo4jMemory, neo4j_driver: object
):
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    with neo4j_driver.session() as session:
        names = {record["name"] for record in session.run("SHOW CONSTRAINTS")}
    assert "event_id" in names
    assert "incident_id" in names


# ---------------------------------------------------------------------------
# the idempotency gate — invariant 5
# ---------------------------------------------------------------------------
def test_writing_the_same_events_twice_creates_no_duplicates(memory: Neo4jMemory):
    """Write 50 events twice, assert exactly 50 nodes.

    This is the test TODO.md 1.1 names explicitly. If it ever fails, every
    confidence score in the system is quietly wrong.
    """
    incident = an_incident()
    memory.upsert_incident(incident)
    events = fifty_events()

    first = memory.write_events(incident.id, events)
    assert len(first) == 50
    assert memory.count_events(incident.id) == 50

    second = memory.write_events(incident.id, events)
    assert len(second) == 50
    assert memory.count_events(incident.id) == 50, (
        "re-collection duplicated evidence — corroboration counts, and "
        "therefore every confidence score, are now inflated"
    )


def test_rewriting_events_does_not_duplicate_part_of_edges(memory: Neo4jMemory):
    """Node count is not enough: duplicated `PART_OF` edges would inflate any
    traversal that counts relationships."""
    incident = an_incident()
    memory.upsert_incident(incident)
    events = fifty_events()
    memory.write_events(incident.id, events)
    memory.write_events(incident.id, events)

    timeline = memory.get_timeline(incident.id)
    assert len(timeline) == 50


def test_upsert_incident_is_idempotent(memory: Neo4jMemory):
    memory.upsert_incident(an_incident())
    memory.upsert_incident(an_incident(title="retitled after review"))
    similar = memory.find_similar_incidents("INC-0001")
    assert similar == []


def test_writing_events_without_an_incident_fails_loudly(memory: Neo4jMemory):
    """A silent no-op here would leave the run analyzing an empty graph."""
    with pytest.raises(GraphStateError, match="not in the graph"):
        memory.write_events("INC-NOPE", fifty_events())


# ---------------------------------------------------------------------------
# round-tripping
# ---------------------------------------------------------------------------
def test_timeline_round_trips_events_in_order(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    timeline = memory.get_timeline(incident.id)
    assert len(timeline) == 50
    timestamps = [event.timestamp for event in timeline]
    assert timestamps == sorted(timestamps)


def test_timestamps_come_back_timezone_aware(memory: Neo4jMemory):
    """A naive datetime out of the graph would violate the UTC invariant."""
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    for event in memory.get_timeline(incident.id):
        assert event.timestamp.tzinfo is not None
        assert event.timestamp.utcoffset() is not None


def test_subtype_attributes_survive_a_round_trip(memory: Neo4jMemory):
    """`Deploy.commit_sha` and `Commit.files_changed` must come back without
    `memory.py` knowing what a deploy is."""
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    by_type = {event.type: event for event in memory.get_timeline(incident.id)}
    commit = by_type[EventType.COMMIT]
    assert commit.attributes["sha"] == "a3f9c1e2"
    assert commit.attributes["files_changed"] == (
        "config/database.py",
        "app/pool.py",
    )
    assert by_type[EventType.DEPLOY].attributes["version"] == "v2.3.1"


def test_count_and_signature_survive_a_round_trip(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    logs = [
        e for e in memory.get_timeline(incident.id) if e.type == EventType.LOG_ERROR
    ]
    assert all(e.signature == "connectionerror_pool_exhausted" for e in logs)
    assert max(e.count for e in logs) == 47


def test_services_are_deduplicated(memory: Neo4jMemory, neo4j_driver: object):
    """All 50 events touch `checkout`; that must be one `Service` node."""
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    with neo4j_driver.session() as session:
        record = session.run("MATCH (s:Service) RETURN count(s) AS n").single()
    assert record is not None
    assert record["n"] == 1


# ---------------------------------------------------------------------------
# temporal chain
# ---------------------------------------------------------------------------
def test_link_temporal_builds_a_consecutive_chain(memory: Neo4jMemory):
    """n events ⇒ n-1 edges. The transitive closure would be O(n²) and add
    nothing the ordering doesn't already say."""
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    assert memory.link_temporal(incident.id) == 49


def test_link_temporal_is_idempotent(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    memory.link_temporal(incident.id)
    assert memory.link_temporal(incident.id) == 49


def test_link_temporal_rebuilds_when_an_event_lands_mid_window(
    memory: Neo4jMemory, neo4j_driver: object
):
    """A late-arriving event must not leave a stale edge that skips over it."""
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    memory.link_temporal(incident.id)

    latecomer = Event(
        id=make_event_id("loki", "late-arrival"),
        type=EventType.LOG_ERROR,
        timestamp=START + timedelta(seconds=15),
        source="loki",
        summary="arrived after the first pass",
        service="checkout",
    )
    memory.write_events(incident.id, [latecomer])
    assert memory.link_temporal(incident.id) == 50

    with neo4j_driver.session() as session:
        record = session.run("MATCH ()-[r:PRECEDES]->() RETURN count(r) AS n").single()
    assert record is not None
    assert record["n"] == 50, "stale PRECEDES edges were left behind"


# ---------------------------------------------------------------------------
# candidate links
# ---------------------------------------------------------------------------
def test_link_candidate_records_which_heuristics_fired(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    events = fifty_events()
    memory.write_events(incident.id, events)

    cause = make_event_id("github", "a3f9c1e2")
    effect = make_event_id("loki", "line-10")
    memory.link_candidate(
        cause, effect, ["temporal_proximity", "change_path_overlap"], 95, 0.55
    )

    chains = memory.get_candidate_chains(incident.id)
    assert len(chains) == 1
    assert set(chains[0].heuristics) == {"temporal_proximity", "change_path_overlap"}
    assert chains[0].heuristic_count == 2
    assert chains[0].delta_seconds == 95


def test_link_candidate_is_idempotent(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    cause = make_event_id("github", "a3f9c1e2")
    effect = make_event_id("loki", "line-10")

    memory.link_candidate(cause, effect, ["temporal_proximity"], 95)
    memory.link_candidate(cause, effect, ["temporal_proximity"], 95)

    assert len(memory.get_candidate_chains(incident.id)) == 1


def test_link_candidate_to_a_missing_event_fails_loudly(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    with pytest.raises(GraphStateError, match="not in the graph"):
        memory.link_candidate(
            make_event_id("github", "a3f9c1e2"), "deadbeefdeadbeef", ["x"], 10
        )


def test_candidate_chains_rank_by_heuristic_count_then_breadth(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    commit = make_event_id("github", "a3f9c1e2")
    deploy = make_event_id("argo", "deploy-991")

    memory.link_candidate(deploy, make_event_id("loki", "line-20"), ["x"], 10)
    memory.link_candidate(
        commit,
        make_event_id("loki", "line-10"),
        ["temporal_proximity", "service_overlap", "change_path_overlap"],
        95,
    )

    chains = memory.get_candidate_chains(incident.id)
    assert chains[0].cause_id == commit, "the 3-heuristic chain should rank first"


def test_candidate_chains_respect_the_limit(memory: Neo4jMemory):
    """The cap is what stops token cost scaling with log volume."""
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    cause = make_event_id("github", "a3f9c1e2")

    for index in range(20):
        memory.link_candidate(
            cause, make_event_id("loki", f"line-{index}"), ["temporal_proximity"], 10
        )

    assert len(memory.get_candidate_chains(incident.id, limit=5)) == 5


# ---------------------------------------------------------------------------
# citation resolution — the project's central claim
# ---------------------------------------------------------------------------
def test_resolve_citations_accepts_real_ids(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    real = make_event_id("loki", "line-3")

    resolved = memory.resolve_citations(incident.id, [real])
    assert resolved[real].valid
    assert resolved[real].actual_timestamp == START + timedelta(seconds=30)


def test_resolve_citations_rejects_fabricated_ids(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    resolved = memory.resolve_citations(incident.id, ["9f2b0000deadbeef"])
    assert not resolved["9f2b0000deadbeef"].valid
    assert resolved["9f2b0000deadbeef"].actual_timestamp is None


def test_resolve_citations_rejects_ids_from_another_incident(memory: Neo4jMemory):
    """A real node cited from the wrong incident is still a fabrication — this
    is why existence and membership are checked in the same query."""
    first = an_incident("INC-0001")
    second = an_incident(
        "INC-0002",
        start_time=START + timedelta(days=1),
        end_time=START + timedelta(days=1, hours=1),
    )
    memory.upsert_incident(first)
    memory.upsert_incident(second)
    memory.write_events(first.id, fifty_events())

    borrowed = make_event_id("loki", "line-3")
    resolved = memory.resolve_citations(second.id, [borrowed])
    assert not resolved[borrowed].valid


def test_resolve_citations_handles_the_whole_document_in_one_call(
    memory: Neo4jMemory,
):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    cited = [make_event_id("loki", f"line-{i}") for i in range(20)]
    cited.append("fabricated000000")
    resolved = memory.resolve_citations(incident.id, cited)

    assert len(resolved) == 21
    assert sum(1 for check in resolved.values() if check.valid) == 20


def test_resolve_citations_deduplicates_repeated_ids(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    real = make_event_id("loki", "line-3")

    resolved = memory.resolve_citations(incident.id, [real, real, real])
    assert len(resolved) == 1


def test_resolve_citations_with_no_citations_is_a_no_op(memory: Neo4jMemory):
    assert memory.resolve_citations("INC-0001", []) == {}


# ---------------------------------------------------------------------------
# hypotheses, fingerprints, recurrence
# ---------------------------------------------------------------------------
def test_persist_hypotheses_writes_support_and_contradiction_edges(
    memory: Neo4jMemory, neo4j_driver: object
):
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())

    statement = "deploy a3f9c1e2 dropped DB_POOL_MAX"
    hypothesis = Hypothesis(
        id=make_hypothesis_id(incident.id, statement),
        rank=1,
        statement=statement,
        supporting_event_ids=(
            make_event_id("github", "a3f9c1e2"),
            make_event_id("loki", "line-1"),
        ),
        contradicting_event_ids=(make_event_id("loki", "line-2"),),
        confidence=0.94,
        band="likely",
    )
    memory.persist_hypotheses(incident.id, [hypothesis], now=START)

    with neo4j_driver.session() as session:
        supports = session.run(
            "MATCH (:Event)-[r:SUPPORTS]->(:Hypothesis) RETURN count(r) AS n"
        ).single()
        contradicts = session.run(
            "MATCH (:Event)-[r:CONTRADICTS]->(:Hypothesis) RETURN count(r) AS n"
        ).single()
    assert supports is not None and supports["n"] == 2
    assert contradicts is not None and contradicts["n"] == 1


def test_persist_hypotheses_is_idempotent(memory: Neo4jMemory, neo4j_driver: object):
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    statement = "deploy a3f9c1e2 dropped DB_POOL_MAX"
    hypothesis = Hypothesis(
        id=make_hypothesis_id(incident.id, statement),
        rank=1,
        statement=statement,
        supporting_event_ids=(make_event_id("github", "a3f9c1e2"),),
        contradicting_event_ids=(),
        confidence=0.94,
        band="likely",
    )

    memory.persist_hypotheses(incident.id, [hypothesis], now=START)
    memory.persist_hypotheses(incident.id, [hypothesis], now=START)

    with neo4j_driver.session() as session:
        record = session.run("MATCH (h:Hypothesis) RETURN count(h) AS n").single()
    assert record is not None
    assert record["n"] == 1


def test_compute_fingerprint_describes_the_causal_pattern(memory: Neo4jMemory):
    incident = an_incident()
    memory.upsert_incident(incident)
    memory.write_events(incident.id, fifty_events())
    memory.link_candidate(
        make_event_id("github", "a3f9c1e2"),
        make_event_id("loki", "line-10"),
        ["change_path_overlap"],
        95,
    )

    fingerprint = memory.compute_fingerprint(incident.id)
    assert fingerprint == ["commit|log_error|checkout|connectionerror_pool_exhausted"]


def test_recurrence_finds_a_structurally_similar_past_incident(memory: Neo4jMemory):
    """The payoff query: a later incident recognising an earlier one."""
    past = an_incident("INC-0001")
    memory.upsert_incident(past)
    memory.write_events(past.id, fifty_events())
    memory.link_candidate(
        make_event_id("github", "a3f9c1e2"),
        make_event_id("loki", "line-10"),
        ["change_path_overlap"],
        95,
    )
    memory.link_candidate(
        make_event_id("argo", "deploy-991"),
        make_event_id("alertmanager", "HighErrorRate:1710252131"),
        ["service_overlap"],
        80,
    )
    memory.compute_fingerprint(past.id)

    later = an_incident(
        "INC-0007",
        start_time=START + timedelta(days=42),
        end_time=START + timedelta(days=42, hours=1),
        fingerprint=(
            "commit|log_error|checkout|connectionerror_pool_exhausted",
            "deploy|alert_fired|checkout|?",
        ),
    )
    memory.upsert_incident(later)

    similar = memory.find_similar_incidents(later.id)
    assert len(similar) == 1
    assert similar[0].incident_id == "INC-0001"
    assert similar[0].overlap == pytest.approx(1.0)
    assert memory.link_similar_incidents(later.id) == 1


def test_recurrence_ignores_incidents_that_have_not_happened_yet(
    memory: Neo4jMemory,
):
    """`past.end_time < i.start_time` — an incident cannot recur from its own
    future."""
    later = an_incident(
        "INC-0007",
        fingerprint=("commit|log_error|checkout|connectionerror_pool_exhausted",),
    )
    future = an_incident(
        "INC-0099",
        start_time=START + timedelta(days=90),
        end_time=START + timedelta(days=90, hours=1),
        fingerprint=("commit|log_error|checkout|connectionerror_pool_exhausted",),
    )
    memory.upsert_incident(later)
    memory.upsert_incident(future)

    assert memory.find_similar_incidents(later.id, min_shared=1) == []
