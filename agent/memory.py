"""The graph. Every line of Cypher in this project lives in this file.

Design rules (docs/02), each load-bearing:

- **Every method takes `incident_id`.** There is no ambient context, so
  cross-incident leakage is structurally impossible rather than merely avoided.
- **Returns dataclasses, never `neo4j.Record`.** If the driver's types escaped
  this module, swapping the graph backend would become a rewrite instead of a
  new class. Kùzu is the documented escape hatch (ADR-002).
- **All writes are idempotent**, `MERGE` on content-derived IDs. Re-running a
  collection must not duplicate evidence — a duplicate silently inflates
  corroboration counts and corrupts every confidence score downstream
  (invariant 5).
- **No LLM imports.** This is data access, tested against a real Neo4j.

Deviation from the doc worth knowing: docs/02 sketches these as module-level
functions. They are a class here so the driver is injected rather than global,
which is what lets the integration tests point at a throwaway testcontainer and
what keeps "no ambient context" true of the connection as well as the queries.
The operations and their signatures are otherwise exactly as documented.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Final, Self

from neo4j import Driver, GraphDatabase
from neo4j.time import DateTime as Neo4jDateTime

from agent.config import Neo4jConfig
from agent.state import (
    EVENT_LABELS,
    RESERVED_EVENT_PROPERTIES,
    Chain,
    CitationCheck,
    CorrectiveActionStatus,
    Event,
    EventType,
    Hypothesis,
    Incident,
    SimilarIncident,
)


class GraphStateError(RuntimeError):
    """Raised when the graph is in a state the caller assumed it wasn't.

    Not named `MemoryError` — that is a builtin, and shadowing it in the one
    module every other module imports would be a genuinely nasty trap.
    """


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
#: Uniqueness on `Event.id` plus content-derived IDs is the whole idempotency
#: story: a re-collected event `MERGE`s onto the same node instead of becoming a
#: second one. Every statement is `IF NOT EXISTS`, so this is safe to run on
#: every startup.
_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    "CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:Event) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT incident_id IF NOT EXISTS "
    "FOR (i:Incident) REQUIRE i.id IS UNIQUE",
    "CREATE CONSTRAINT service_name IF NOT EXISTS "
    "FOR (s:Service) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT hypo_id IF NOT EXISTS FOR (h:Hypothesis) REQUIRE h.id IS UNIQUE",
    "CREATE INDEX event_ts IF NOT EXISTS FOR (e:Event) ON (e.timestamp)",
    "CREATE INDEX event_sig IF NOT EXISTS FOR (e:Event) ON (e.signature)",
    "CREATE INDEX event_type IF NOT EXISTS FOR (e:Event) ON (e.type)",
)


# ---------------------------------------------------------------------------
# record → dataclass
# ---------------------------------------------------------------------------
def _to_datetime(value: object) -> datetime | None:
    """Neo4j temporal → aware `datetime`. Anything naive coming back out would
    violate the UTC invariant, so it is rejected rather than coerced."""
    if value is None:
        return None
    if isinstance(value, Neo4jDateTime):
        native = value.to_native()
    elif isinstance(value, datetime):
        native = value
    else:
        raise GraphStateError(f"expected a temporal value, got {type(value).__name__}")
    if native.tzinfo is None:
        raise GraphStateError(f"graph returned a naive datetime: {native!r}")
    return native


def _tuple_of_str(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _event_from_properties(props: Mapping[str, Any]) -> Event:
    """Rebuild an `Event` from a node's property map.

    Anything not in `RESERVED_EVENT_PROPERTIES` is a subtype property and goes
    back into `attributes`, which is how `Deploy.commit_sha` survives a
    round trip without this module knowing what a deploy is.
    """
    timestamp = _to_datetime(props.get("timestamp"))
    if timestamp is None:
        raise GraphStateError(f"event {props.get('id')!r} has no timestamp")

    raw_json = props.get("raw_json")
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else {}
    except json.JSONDecodeError:
        raw = {"_unparseable": raw_json}

    attributes = {
        key: (tuple(value) if isinstance(value, list) else value)
        for key, value in props.items()
        if key not in RESERVED_EVENT_PROPERTIES
    }

    return Event(
        id=str(props["id"]),
        type=EventType(props["type"]),
        timestamp=timestamp,
        source=str(props.get("source", "")),
        summary=str(props.get("summary", "")),
        service=props.get("service"),
        signature=props.get("signature"),
        count=int(props.get("count", 1) or 1),
        attributes=attributes,
        raw=raw if isinstance(raw, dict) else {"value": raw},
    )


def _event_to_row(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": str(event.type),
        "timestamp": event.timestamp,
        "source": event.source,
        "service": event.service,
        "signature": event.signature,
        "summary": event.summary,
        "raw_json": event.raw_json(),
        "count": event.count,
        # tuples must become lists for the driver's type mapping
        "attributes": {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in event.attributes.items()
        },
    }


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class Neo4jMemory:
    """Long-term memory. Construct with a `Neo4jConfig`, close when done."""

    def __init__(self, driver: Driver, database: str | None = None) -> None:
        self._driver = driver
        self._database = database

    @classmethod
    def from_config(cls, config: Neo4jConfig, database: str | None = None) -> Self:
        driver = GraphDatabase.driver(config.uri, auth=(config.user, config.password))
        return cls(driver, database=database)

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()

    # -- schema lifecycle ---------------------------------------------------
    def ensure_schema(self) -> None:
        """Create constraints and indexes. Idempotent; safe on every startup."""
        with self._driver.session(database=self._database) as session:
            for statement in _SCHEMA_STATEMENTS:
                session.run(statement)

    # -- write path (evidence plane) ----------------------------------------
    def upsert_incident(self, incident: Incident) -> str:
        query = """
        MERGE (i:Incident {id: $id})
        SET i.title = $title,
            i.start_time = $start_time,
            i.end_time = $end_time,
            i.severity = $severity,
            i.status = $status
        FOREACH (_ IN CASE WHEN $fingerprint = [] THEN [] ELSE [1] END |
            SET i.fingerprint = $fingerprint
        )
        RETURN i.id AS id
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(
                query,
                id=incident.id,
                title=incident.title,
                start_time=incident.start_time,
                end_time=incident.end_time,
                severity=incident.severity,
                status=incident.status,
                fingerprint=list(incident.fingerprint),
            ).single()
        if record is None:
            raise GraphStateError(f"failed to upsert incident {incident.id}")
        return str(record["id"])

    def write_events(self, incident_id: str, events: Sequence[Event]) -> list[str]:
        """`MERGE` events, attach `:PART_OF` and `:AFFECTS`.

        Writing the same events twice must leave the graph identical — that is
        the property the integration test pins, because a duplicate would
        silently inflate every corroboration count.
        """
        if not events:
            return []
        self._require_incident(incident_id)

        written: list[str] = []
        with self._driver.session(database=self._database) as session:
            # Grouped by type because a node label cannot be a query parameter.
            # The label is interpolated only from EVENT_LABELS, never from input.
            for event_type, batch in _group_by_type(events):
                label = EVENT_LABELS[event_type]
                query = f"""
                MATCH (i:Incident {{id: $incidentId}})
                UNWIND $rows AS row
                MERGE (e:Event {{id: row.id}})
                SET e:{label}
                SET e.type = row.type,
                    e.timestamp = row.timestamp,
                    e.source = row.source,
                    e.service = row.service,
                    e.signature = row.signature,
                    e.summary = row.summary,
                    e.raw_json = row.raw_json,
                    e.count = row.count
                SET e += row.attributes
                MERGE (e)-[:PART_OF]->(i)
                FOREACH (_ IN CASE WHEN row.service IS NULL THEN [] ELSE [1] END |
                    MERGE (s:Service {{name: row.service}})
                    MERGE (e)-[:AFFECTS]->(s)
                )
                RETURN e.id AS id
                """
                result = session.run(
                    query,
                    incidentId=incident_id,
                    rows=[_event_to_row(event) for event in batch],
                )
                written.extend(str(record["id"]) for record in result)
        return written

    def link_temporal(self, incident_id: str) -> int:
        """Build the `PRECEDES` chain over consecutive events.

        Consecutive only — the transitive closure is O(n²) edges and says
        nothing the ordering doesn't already say.

        Existing edges are dropped first so that adding an event mid-window
        rebuilds a correct chain rather than leaving a stale link that skips it.
        """
        with self._driver.session(database=self._database) as session:
            session.run(
                """
                MATCH (:Event)-[:PART_OF]->(i:Incident {id: $incidentId})
                WITH i
                MATCH (a:Event)-[r:PRECEDES]->(b:Event)
                WHERE (a)-[:PART_OF]->(i) AND (b)-[:PART_OF]->(i)
                DELETE r
                """,
                incidentId=incident_id,
            )
            record = session.run(
                """
                MATCH (e:Event)-[:PART_OF]->(:Incident {id: $incidentId})
                WITH e ORDER BY e.timestamp ASC, e.id ASC
                WITH collect(e) AS events
                WITH events, size(events) AS n
                WHERE n > 1
                UNWIND range(0, n - 2) AS idx
                WITH events[idx] AS a, events[idx + 1] AS b
                MERGE (a)-[r:PRECEDES]->(b)
                SET r.delta_seconds =
                    duration.inSeconds(a.timestamp, b.timestamp).seconds
                RETURN count(r) AS linked
                """,
                incidentId=incident_id,
            ).single()
        return int(record["linked"]) if record else 0

    def link_candidate(
        self,
        cause_id: str,
        effect_id: str,
        heuristics: Sequence[str],
        delta_seconds: int,
        score: float = 0.0,
    ) -> None:
        """Record one `POSSIBLY_CAUSED` edge and which heuristics produced it."""
        query = """
        MATCH (cause:Event {id: $causeId})
        MATCH (effect:Event {id: $effectId})
        MERGE (cause)-[r:POSSIBLY_CAUSED]->(effect)
        SET r.heuristics = $heuristics,
            r.delta_seconds = $deltaSeconds,
            r.score = $score
        RETURN r
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(
                query,
                causeId=cause_id,
                effectId=effect_id,
                heuristics=list(heuristics),
                deltaSeconds=delta_seconds,
                score=score,
            ).single()
        if record is None:
            raise GraphStateError(
                f"cannot link {cause_id} -> {effect_id}: one or both events "
                "are not in the graph"
            )

    # -- read path (reasoning plane) ----------------------------------------
    def get_timeline(self, incident_id: str) -> list[Event]:
        """Every event for the incident, in order. The Timeline section of the
        postmortem is rendered from this by code — there is no reason to pay a
        model to sort, and doing so is the largest hallucination surface in the
        document."""
        query = """
        MATCH (e:Event)-[:PART_OF]->(:Incident {id: $incidentId})
        RETURN properties(e) AS props
        ORDER BY e.timestamp ASC, e.id ASC
        """
        with self._driver.session(database=self._database) as session:
            return [
                _event_from_properties(record["props"])
                for record in session.run(query, incidentId=incident_id)
            ]

    def get_candidate_chains(self, incident_id: str, limit: int = 15) -> list[Chain]:
        """Candidate causes ranked by corroboration, capped at `limit`.

        The cap is not cosmetic: this is what the Analyst sees, so an unbounded
        result would make token cost scale with log volume.
        """
        query = """
        MATCH (cause:Event)-[r:POSSIBLY_CAUSED]->(effect:Event)-[:PART_OF]
              ->(i:Incident {id: $incidentId})
        OPTIONAL MATCH (cause)-[:POSSIBLY_CAUSED]->(other:Event)-[:PART_OF]->(i)
        WHERE other <> effect
        WITH cause, effect, r,
             count(DISTINCT other) AS breadth,
             size(r.heuristics) AS heuristic_count,
             collect(DISTINCT other.type) AS effect_types
        RETURN cause.id AS cause_id, cause.summary AS cause_summary,
               effect.id AS effect_id, effect.summary AS effect_summary,
               r.heuristics AS heuristics, r.delta_seconds AS delta_seconds,
               breadth, heuristic_count, effect_types
        ORDER BY heuristic_count DESC, breadth DESC, r.delta_seconds ASC
        LIMIT $limit
        """
        with self._driver.session(database=self._database) as session:
            return [
                Chain(
                    cause_id=str(record["cause_id"]),
                    cause_summary=str(record["cause_summary"] or ""),
                    effect_id=str(record["effect_id"]),
                    effect_summary=str(record["effect_summary"] or ""),
                    heuristics=_tuple_of_str(record["heuristics"]),
                    delta_seconds=int(record["delta_seconds"] or 0),
                    breadth=int(record["breadth"] or 0),
                    effect_types=_tuple_of_str(record["effect_types"]),
                )
                for record in session.run(query, incidentId=incident_id, limit=limit)
            ]

    def find_similar_incidents(
        self, incident_id: str, min_shared: int = 2
    ) -> list[SimilarIncident]:
        """Past incidents sharing ≥ `min_shared` fingerprint components.

        Returns dataclasses rather than the `list[dict]` docs/02 sketches — the
        "never leak driver shapes" rule applies to loose dicts too.
        """
        query = """
        MATCH (i:Incident {id: $incidentId})
        MATCH (past:Incident)
        WHERE past.id <> i.id AND past.end_time < i.start_time
        WITH i, past,
             [x IN coalesce(i.fingerprint, [])
              WHERE x IN coalesce(past.fingerprint, [])] AS shared
        WHERE size(shared) >= $minShared
        OPTIONAL MATCH (past)-[:REMEDIATED_BY]->(ca:CorrectiveAction)
        WITH i, past, shared,
             collect({action: ca.description, status: ca.status}) AS actions
        RETURN past.id AS id, past.title AS title, past.end_time AS end_time,
               shared,
               toFloat(size(shared)) / size(coalesce(i.fingerprint, [1]))
                   AS overlap,
               actions
        ORDER BY overlap DESC
        LIMIT 5
        """
        with self._driver.session(database=self._database) as session:
            records = list(
                session.run(query, incidentId=incident_id, minShared=min_shared)
            )

        return [
            SimilarIncident(
                incident_id=str(record["id"]),
                title=str(record["title"] or ""),
                end_time=_to_datetime(record["end_time"]),
                shared_components=_tuple_of_str(record["shared"]),
                overlap=float(record["overlap"] or 0.0),
                corrective_actions=tuple(
                    CorrectiveActionStatus(
                        description=str(action["action"]),
                        status=str(action.get("status") or "unknown"),
                    )
                    for action in record["actions"]
                    if action and action.get("action")
                ),
            )
            for record in records
        ]

    # -- verification plane --------------------------------------------------
    def resolve_citations(
        self, incident_id: str, cited_ids: Sequence[str]
    ) -> dict[str, CitationCheck]:
        """Resolve every cited ID in one round trip.

        This is the query the project's central claim rests on. It checks
        existence **and** incident membership together, because a citation to a
        real node belonging to a different incident is still a fabrication.
        """
        if not cited_ids:
            return {}
        query = """
        UNWIND $citedIds AS cid
        OPTIONAL MATCH (e:Event {id: cid})-[:PART_OF]->(:Incident {id: $incidentId})
        RETURN cid, e IS NOT NULL AS valid, e.timestamp AS actual_ts
        """
        unique_ids = list(dict.fromkeys(cited_ids))
        with self._driver.session(database=self._database) as session:
            records = list(
                session.run(query, incidentId=incident_id, citedIds=unique_ids)
            )
        return {
            str(record["cid"]): CitationCheck(
                cited_id=str(record["cid"]),
                valid=bool(record["valid"]),
                actual_timestamp=_to_datetime(record["actual_ts"]),
            )
            for record in records
        }

    # -- post-run ------------------------------------------------------------
    def persist_hypotheses(
        self, incident_id: str, hypotheses: Sequence[Hypothesis], now: datetime
    ) -> None:
        """Store hypotheses and their evidence edges, so past reasoning stays
        auditable.

        No `EXPLAINS` edge is created: docs/02 reserves it for a hypothesis
        *promoted to accepted root cause*, which is a human decision this
        pipeline does not make. Attribution is carried by `incident_id` on the
        node instead of inventing a relationship the schema doesn't define.
        """
        if not hypotheses:
            return
        self._require_incident(incident_id)

        rows = [
            {
                "id": h.id,
                "statement": h.statement,
                "confidence": h.confidence,
                "band": h.band,
                "rank": h.rank,
                "supporting": list(h.supporting_event_ids),
                "contradicting": list(h.contradicting_event_ids),
            }
            for h in hypotheses
        ]

        with self._driver.session(database=self._database) as session:
            session.run(
                """
                UNWIND $rows AS row
                MERGE (h:Hypothesis {id: row.id})
                ON CREATE SET h.created_at = $now
                SET h.statement = row.statement,
                    h.confidence = row.confidence,
                    h.band = row.band,
                    h.rank = row.rank,
                    h.incident_id = $incidentId
                """,
                rows=rows,
                incidentId=incident_id,
                now=now,
            )
            session.run(
                """
                UNWIND $rows AS row
                MATCH (h:Hypothesis {id: row.id})
                UNWIND row.supporting AS eid
                MATCH (e:Event {id: eid})-[:PART_OF]->(:Incident {id: $incidentId})
                MERGE (e)-[:SUPPORTS]->(h)
                """,
                rows=[r for r in rows if r["supporting"]],
                incidentId=incident_id,
            )
            session.run(
                """
                UNWIND $rows AS row
                MATCH (h:Hypothesis {id: row.id})
                UNWIND row.contradicting AS eid
                MATCH (e:Event {id: eid})-[:PART_OF]->(:Incident {id: $incidentId})
                MERGE (e)-[:CONTRADICTS]->(h)
                """,
                rows=[r for r in rows if r["contradicting"]],
                incidentId=incident_id,
            )

    def compute_fingerprint(self, incident_id: str) -> list[str]:
        """Fingerprint the incident's causal pattern and store it on the node.

        Each component is `cause_type|effect_type|service|signature`. Recurrence
        matching is set overlap on these, which is why it survives the raw
        events being dropped by the retention policy.
        """
        query = """
        MATCH (cause:Event)-[:POSSIBLY_CAUSED]->(effect:Event)
        MATCH (i:Incident {id: $incidentId})
        WHERE (cause)-[:PART_OF]->(i) AND (effect)-[:PART_OF]->(i)
        WITH DISTINCT cause.type + '|' + effect.type + '|' +
             coalesce(effect.service, '?') + '|' +
             coalesce(effect.signature, '?') AS component
        ORDER BY component
        RETURN collect(component) AS fingerprint
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(query, incidentId=incident_id).single()
            fingerprint = _tuple_of_str(record["fingerprint"]) if record else ()
            session.run(
                "MATCH (i:Incident {id: $incidentId}) SET i.fingerprint = $fp",
                incidentId=incident_id,
                fp=list(fingerprint),
            )
        return list(fingerprint)

    def link_similar_incidents(self, incident_id: str, min_shared: int = 2) -> int:
        """Persist `SIMILAR_TO` edges to structurally similar past incidents."""
        query = """
        MATCH (i:Incident {id: $incidentId})
        MATCH (past:Incident)
        WHERE past.id <> i.id AND past.end_time < i.start_time
        WITH i, past,
             [x IN coalesce(i.fingerprint, [])
              WHERE x IN coalesce(past.fingerprint, [])] AS shared
        WHERE size(shared) >= $minShared
        MERGE (i)-[s:SIMILAR_TO]->(past)
        SET s.overlap = toFloat(size(shared)) / size(coalesce(i.fingerprint, [1])),
            s.matched_on = shared
        RETURN count(s) AS linked
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(
                query, incidentId=incident_id, minShared=min_shared
            ).single()
        return int(record["linked"]) if record else 0

    # -- internals -----------------------------------------------------------
    def _require_incident(self, incident_id: str) -> None:
        """Fail loudly rather than write zero rows.

        Without this, a `MATCH (i:Incident ...)` that finds nothing makes the
        whole `UNWIND` produce no rows — the write silently becomes a no-op and
        the run continues against an empty graph.
        """
        with self._driver.session(database=self._database) as session:
            record = session.run(
                "MATCH (i:Incident {id: $incidentId}) RETURN i.id AS id",
                incidentId=incident_id,
            ).single()
        if record is None:
            raise GraphStateError(
                f"incident {incident_id!r} is not in the graph; "
                "call upsert_incident() before writing evidence"
            )

    def count_events(self, incident_id: str) -> int:
        """Event count for the incident. Used by the run report and by the
        idempotency test that pins invariant 5."""
        with self._driver.session(database=self._database) as session:
            record = session.run(
                """
                MATCH (e:Event)-[:PART_OF]->(:Incident {id: $incidentId})
                RETURN count(e) AS n
                """,
                incidentId=incident_id,
            ).single()
        return int(record["n"]) if record else 0


def _group_by_type(
    events: Iterable[Event],
) -> Iterator[tuple[EventType, list[Event]]]:
    grouped: dict[EventType, list[Event]] = {}
    for event in events:
        grouped.setdefault(event.type, []).append(event)
    return iter(grouped.items())
