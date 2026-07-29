"""Recurrence memory against real Neo4j: writing corrective actions, and the
full loop from a published document back into the *next* incident's document.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.memory import GraphStateError, Neo4jMemory
from agent.nodes.recurrence import persist_corrective_actions
from agent.render.recurrence import render_similar_incidents
from agent.state import Event, EventType, Incident, make_corrective_action_id

pytestmark = pytest.mark.integration

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)


def an_incident(incident_id: str, start: datetime) -> Incident:
    return Incident(
        id=incident_id,
        title="checkout 500s after deploy",
        start_time=start,
        end_time=start + timedelta(hours=1),
        severity="sev2",
    )


def a_causal_pair(prefix: str, at: datetime) -> list[Event]:
    """One commit causing a log_error and an alert — two fingerprint
    components, so overlap can clear the default `min_shared=2` the same way
    a real incident's multi-symptom fingerprint would."""
    return [
        Event(
            id=f"{prefix}commitaaaaaaaa",
            type=EventType.COMMIT,
            timestamp=at,
            source="git",
            summary="remove DB_POOL_MAX",
            service="checkout",
        ),
        Event(
            id=f"{prefix}logerrorbbbbbb",
            type=EventType.LOG_ERROR,
            timestamp=at + timedelta(seconds=40),
            source="logs",
            summary="pool exhausted",
            service="checkout",
            signature="connectionerror: pool exhausted (<num>/<num>)",
        ),
        Event(
            id=f"{prefix}alertccccccccc",
            type=EventType.ALERT_FIRED,
            timestamp=at + timedelta(minutes=2),
            source="alertmanager",
            summary="HighErrorRate firing",
            service="checkout",
            signature="higherrorrate firing",
        ),
    ]


# ---------------------------------------------------------------------------
# add_corrective_action
# ---------------------------------------------------------------------------
def test_add_corrective_action_creates_the_node_and_edge(
    memory: Neo4jMemory, neo4j_driver: object
):
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    incident = an_incident("INC-0001", START)
    memory.upsert_incident(incident)
    action_id = make_corrective_action_id(incident.id, "Add a pool size alert")

    result = memory.add_corrective_action(
        incident.id, action_id, "Add a pool size alert", status="open"
    )
    assert result == action_id

    with neo4j_driver.session() as session:
        record = session.run(
            "MATCH (i:Incident {id: $id})-[:REMEDIATED_BY]->(ca:CorrectiveAction) "
            "RETURN ca.description AS d, ca.status AS s",
            id=incident.id,
        ).single()
    assert record is not None
    assert record["d"] == "Add a pool size alert"
    assert record["s"] == "open"


def test_add_corrective_action_is_idempotent(memory: Neo4jMemory, neo4j_driver: object):
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    incident = an_incident("INC-0001", START)
    memory.upsert_incident(incident)
    action_id = make_corrective_action_id(incident.id, "Add a pool size alert")

    memory.add_corrective_action(incident.id, action_id, "Add a pool size alert")
    memory.add_corrective_action(incident.id, action_id, "Add a pool size alert")

    with neo4j_driver.session() as session:
        record = session.run(
            "MATCH (ca:CorrectiveAction) RETURN count(ca) AS n"
        ).single()
    assert record is not None
    assert record["n"] == 1


def test_add_corrective_action_never_reopens_a_closed_one(
    memory: Neo4jMemory, neo4j_driver: object
):
    """Completion is a fact about the world this pipeline cannot observe.
    Re-extracting the same bullet from a later run must not silently flip a
    human-marked "done" back to "open"."""
    from neo4j import Driver

    assert isinstance(neo4j_driver, Driver)
    incident = an_incident("INC-0001", START)
    memory.upsert_incident(incident)
    action_id = make_corrective_action_id(incident.id, "Add a pool size alert")
    memory.add_corrective_action(incident.id, action_id, "Add a pool size alert")

    # a human marks it done, out of band — not through this pipeline
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (ca:CorrectiveAction {id: $id}) SET ca.status = 'done'",
            id=action_id,
        )

    # the same bullet gets re-extracted from a later run's document
    memory.add_corrective_action(incident.id, action_id, "Add a pool size alert")

    with neo4j_driver.session() as session:
        record = session.run(
            "MATCH (ca:CorrectiveAction {id: $id}) RETURN ca.status AS s", id=action_id
        ).single()
    assert record is not None
    assert record["s"] == "done", "re-writing must not silently reopen a closed action"


def test_add_corrective_action_requires_the_incident_to_exist(memory: Neo4jMemory):
    with pytest.raises(GraphStateError, match="not in the graph"):
        memory.add_corrective_action("INC-NOPE", "ca:x", "do the thing")


# ---------------------------------------------------------------------------
# persist_corrective_actions
# ---------------------------------------------------------------------------
DOCUMENT = """
## Summary

The service failed [src:aaacommitaaaaaaaa].

## Corrective Actions

- Add a connection pool size alert.
- Restore the DB_POOL_MAX default explicitly.
"""


def test_persist_corrective_actions_writes_every_bullet(memory: Neo4jMemory):
    incident = an_incident("INC-0001", START)
    memory.upsert_incident(incident)

    ids = persist_corrective_actions(memory, incident.id, DOCUMENT)
    assert len(ids) == 2

    similar = memory.find_similar_incidents(
        "INC-0001", min_shared=0
    )  # nothing later exists yet; just prove no crash
    assert similar == []


def test_persist_corrective_actions_on_a_document_with_none_is_a_no_op(
    memory: Neo4jMemory,
):
    incident = an_incident("INC-0001", START)
    memory.upsert_incident(incident)
    document = "## Summary\n\nThe service failed [src:x].\n"
    assert persist_corrective_actions(memory, incident.id, document) == []


# ---------------------------------------------------------------------------
# the full loop: a published document's actions surface in the NEXT incident
# ---------------------------------------------------------------------------
def test_an_open_action_from_a_past_incident_surfaces_in_the_next_one(
    memory: Neo4jMemory,
):
    """The money feature, end to end: incident A ships with an open corrective
    action; incident B, six weeks later, has the same causal fingerprint; B's
    recurrence section must name A and say the fix is still open."""
    past = an_incident("INC-0001", START)
    memory.upsert_incident(past)
    memory.write_events(past.id, a_causal_pair("aaa", START))
    memory.link_candidate(
        "aaacommitaaaaaaaa", "aaalogerrorbbbbbb", ["change_path_overlap"], 40
    )
    memory.link_candidate(
        "aaacommitaaaaaaaa", "aaaalertccccccccc", ["service_overlap"], 120
    )
    memory.compute_fingerprint(past.id)
    persist_corrective_actions(memory, past.id, DOCUMENT)

    later_start = START + timedelta(weeks=6)
    later = an_incident("INC-0007", later_start)
    memory.upsert_incident(later)
    memory.write_events(later.id, a_causal_pair("bbb", later_start))
    memory.link_candidate(
        "bbbcommitaaaaaaaa", "bbblogerrorbbbbbb", ["change_path_overlap"], 40
    )
    memory.link_candidate(
        "bbbcommitaaaaaaaa", "bbbalertccccccccc", ["service_overlap"], 120
    )
    memory.compute_fingerprint(later.id)

    similar = memory.find_similar_incidents(later.id)
    assert len(similar) == 1
    assert similar[0].incident_id == "INC-0001"
    open_descriptions = {
        a.description for a in similar[0].corrective_actions if a.is_open
    }
    assert open_descriptions == {
        "Add a connection pool size alert.",
        "Restore the DB_POOL_MAX default explicitly.",
    }

    section = render_similar_incidents(similar)
    assert "INC-0001" in section
    assert "2 open corrective actions" in section
    assert "Add a connection pool size alert." in section
