"""The validator against real Neo4j.

The unit tests hand the validator a dictionary. This hands it the actual
Cypher, because the project's central claim is that citations are resolved
*against the graph* — and a resolver that only ever met a dict has not been
shown to do that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.memory import Neo4jMemory
from agent.nodes.validator import validate
from agent.state import ComplaintKind, Event, EventType, Incident

pytestmark = pytest.mark.integration

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)


def seed(memory: Neo4jMemory, incident_id: str, event_id: str) -> Event:
    incident = Incident(
        id=incident_id,
        title="checkout 500s",
        start_time=START,
        end_time=START + timedelta(hours=1),
    )
    memory.upsert_incident(incident)
    event = Event(
        id=event_id,
        type=EventType.COMMIT,
        timestamp=START + timedelta(seconds=40),
        source="git",
        summary="remove DB_POOL_MAX",
        service="checkout",
    )
    memory.write_events(incident_id, [event])
    return event


def test_a_real_citation_resolves_against_the_graph(memory: Neo4jMemory):
    seed(memory, "INC-0001", "a3f9c1e2b4d6f001")
    report = validate(
        incident_id="INC-0001",
        document="## Summary\n\nThe setting was removed [src:a3f9c1e2b4d6f001].\n",
        resolve=lambda ids: memory.resolve_citations("INC-0001", list(ids)),
        coverage_threshold=0.95,
    )
    assert report.passed
    assert report.citations_hallucinated == 0


def test_a_fabricated_citation_is_caught_by_the_graph(memory: Neo4jMemory):
    """No regex could catch this: `9f2b000000000000` looks exactly like an ID."""
    seed(memory, "INC-0001", "a3f9c1e2b4d6f001")
    report = validate(
        incident_id="INC-0001",
        document="## Summary\n\nThe setting was removed [src:9f2b000000000000].\n",
        resolve=lambda ids: memory.resolve_citations("INC-0001", list(ids)),
        coverage_threshold=0.95,
    )
    assert not report.passed
    assert report.citations_hallucinated == 1


def test_a_real_event_from_another_incident_is_rejected(memory: Neo4jMemory):
    """The cross-incident case, end to end. The node genuinely exists; it just
    does not belong to the incident being written about."""
    seed(memory, "INC-0001", "a3f9c1e2b4d6f001")
    other = Incident(
        id="INC-0002",
        title="unrelated",
        start_time=START + timedelta(days=1),
        end_time=START + timedelta(days=1, hours=1),
    )
    memory.upsert_incident(other)

    report = validate(
        incident_id="INC-0002",
        document="## Summary\n\nThe setting was removed [src:a3f9c1e2b4d6f001].\n",
        resolve=lambda ids: memory.resolve_citations("INC-0002", list(ids)),
        coverage_threshold=0.95,
    )
    assert report.citations_hallucinated == 1
    assert not report.passed


def test_timestamp_mismatch_is_caught_against_the_real_node(memory: Neo4jMemory):
    seed(memory, "INC-0001", "a3f9c1e2b4d6f001")
    document = (
        "## Summary\n\nThe setting was removed at 09:15 "
        "[src:a3f9c1e2b4d6f001, 2026-03-12T09:15:00Z].\n"
    )
    report = validate(
        incident_id="INC-0001",
        document=document,
        resolve=lambda ids: memory.resolve_citations("INC-0001", list(ids)),
        coverage_threshold=0.95,
    )
    assert ComplaintKind.TIMESTAMP_MISMATCH in {c.kind for c in report.complaints}
    assert not report.passed


def test_the_correct_timestamp_from_the_graph_passes(memory: Neo4jMemory):
    event = seed(memory, "INC-0001", "a3f9c1e2b4d6f001")
    stamp = event.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    report = validate(
        incident_id="INC-0001",
        document=(
            f"## Summary\n\nThe setting was removed [src:a3f9c1e2b4d6f001, {stamp}].\n"
        ),
        resolve=lambda ids: memory.resolve_citations("INC-0001", list(ids)),
        coverage_threshold=0.95,
    )
    assert report.passed
