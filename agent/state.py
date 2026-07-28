"""Domain objects and the LangGraph pipeline state.

These are the contracts every plane agrees on. Collectors produce `Event`s, the
graph stores them, the Analyst references them *by ID only*, and the Validator
resolves those IDs back against the graph. Nothing in the reasoning plane may
introduce a fact that isn't reachable from one of these objects — that is
invariant 1, expressed as a type.

Values are frozen dataclasses. Sequence fields are tuples rather than lists so a
constructed object cannot be mutated out from under the code that trusts it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, TypedDict

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """The five shapes of evidence. New source ⇒ new `Collector`, not a new type
    here unless the evidence is genuinely a different kind of thing."""

    LOG_ERROR = "log_error"
    ALERT_FIRED = "alert_fired"
    DEPLOY = "deploy"
    COMMIT = "commit"
    CHAT_MESSAGE = "chat_message"


#: Dual-labelling (`:Event:Deploy`) per docs/02: generic timeline queries hit
#: `:Event`, type-specific heuristics hit the subtype. Labels cannot be
#: parameterized in Cypher, so `memory.py` interpolates them — it may only ever
#: do so from this mapping, never from caller input.
EVENT_LABELS: Final[Mapping[EventType, str]] = {
    EventType.LOG_ERROR: "LogEntry",
    EventType.ALERT_FIRED: "Alert",
    EventType.DEPLOY: "Deploy",
    EventType.COMMIT: "Commit",
    EventType.CHAT_MESSAGE: "ChatMessage",
}

Band = Literal["likely", "plausible", "tentative"]

#: Names of the linker heuristics that can fire on a `POSSIBLY_CAUSED` edge.
#: These are the *evidence* names; the confidence model's signal names are a
#: superset (it also scores `metric_correlation` and `human_confirmation`).
HEURISTIC_NAMES: Final[frozenset[str]] = frozenset(
    {"temporal_proximity", "service_overlap", "change_path_overlap"}
)

#: Property names `memory.py` writes on every `:Event` node. `Event.attributes`
#: may not collide with these — a subtype property that silently overwrote
#: `timestamp` would corrupt the timeline and every citation check against it.
RESERVED_EVENT_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        "id",
        "type",
        "timestamp",
        "source",
        "service",
        "signature",
        "summary",
        "raw_json",
        "count",
    }
)

Primitive = str | int | float | bool | None
#: Neo4j stores primitives and homogeneous lists of primitives. Anything richer
#: belongs in `Event.raw`, which is serialized as an opaque JSON string.
AttributeValue = Primitive | tuple[str, ...] | tuple[int, ...] | tuple[float, ...]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{label} must be timezone-aware; naive datetimes are a bug "
            "(CLAUDE.md: all timestamps UTC, timezone-aware)"
        )


def make_event_id(source: str, native_id: str) -> str:
    """Content-derived event ID: `sha256(source:native_id)[:16]`.

    Invariant 5 — idempotency is load-bearing. Re-collecting the same evidence
    must `MERGE` onto the same node, because a duplicate silently inflates
    corroboration counts and therefore every confidence score downstream.

    The separator matters: without it `("ab", "c")` and `("a", "bc")` would
    collide, which is exactly the sort of bug that surfaces months later as an
    unexplained score.
    """
    if not source or not native_id:
        raise ValueError(
            f"event ID needs both source and native_id, got {source!r}/{native_id!r}"
        )
    digest = hashlib.sha256(f"{source}:{native_id}".encode())
    return digest.hexdigest()[:16]


def make_hypothesis_id(incident_id: str, statement: str) -> str:
    """Content-derived and namespaced by incident, so re-running a completed
    analysis updates its hypotheses instead of accumulating near-duplicates."""
    digest = hashlib.sha256(f"{incident_id}:{statement}".encode())
    return f"hyp:{digest.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Window:
    """A closed incident window. Collectors are asked for evidence inside it."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_aware(self.start, "Window.start")
        _require_aware(self.end, "Window.end")
        if self.end < self.start:
            raise ValueError(f"Window.end {self.end} precedes start {self.start}")

    def contains(self, moment: datetime) -> bool:
        _require_aware(moment, "moment")
        return self.start <= moment <= self.end


@dataclass(frozen=True, slots=True)
class Event:
    """One timestamped piece of evidence — the atom everything else cites."""

    id: str
    type: EventType
    timestamp: datetime
    source: str
    summary: str
    service: str | None = None
    signature: str | None = None
    #: Occurrence count. Signature collapsing folds thousands of identical log
    #: lines into one node, and this is how many it stood for.
    count: int = 1
    #: Normalized subtype properties written as graph properties —
    #: `commit_sha`, `rule_name`, `files_changed`, and so on (docs/02). Kept
    #: generic so `memory.py` never learns the shape of any single source.
    attributes: Mapping[str, AttributeValue] = field(
        default_factory=dict, compare=False
    )
    #: The untouched original payload, stored opaquely. Never parsed by the
    #: reasoning plane — it exists for a human debugging a bad extraction.
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, f"Event[{self.id}].timestamp")
        if not self.id:
            raise ValueError("Event.id must not be empty")
        if self.count < 1:
            raise ValueError(f"Event[{self.id}].count must be >= 1, got {self.count}")
        collisions = RESERVED_EVENT_PROPERTIES & self.attributes.keys()
        if collisions:
            raise ValueError(
                f"Event[{self.id}].attributes may not shadow base properties: "
                f"{sorted(collisions)}"
            )
        for key, value in self.attributes.items():
            if isinstance(value, tuple):
                if not all(isinstance(item, (str, int, float, bool)) for item in value):
                    raise ValueError(
                        f"Event[{self.id}].attributes[{key!r}] must be a tuple of "
                        "primitives; richer structures belong in .raw"
                    )
            elif not isinstance(value, (str, int, float, bool, type(None))):
                raise ValueError(
                    f"Event[{self.id}].attributes[{key!r}] is {type(value).__name__}; "
                    "only primitives and tuples of primitives are storable"
                )

    def raw_json(self) -> str:
        """`raw` serialized for storage. Falls back to `repr` for values JSON
        can't express, because losing the node over an odd payload would be a
        worse outcome than a lossy debug field."""
        return json.dumps(self.raw, default=repr, sort_keys=True)


@dataclass(frozen=True, slots=True)
class Incident:
    """The investigation unit. Every memory call is scoped by one of these."""

    id: str
    title: str
    start_time: datetime
    end_time: datetime
    severity: str = "unknown"
    status: str = "closed"
    #: Structural signature of the causal pattern, computed post-run. Empty
    #: until `compute_fingerprint()` has run; it is what recurrence matches on.
    fingerprint: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.start_time, f"Incident[{self.id}].start_time")
        _require_aware(self.end_time, f"Incident[{self.id}].end_time")
        if self.end_time < self.start_time:
            raise ValueError(
                f"Incident[{self.id}] ends {self.end_time} before it starts "
                f"{self.start_time}"
            )
        if not self.id:
            raise ValueError("Incident.id must not be empty")

    @property
    def window(self) -> Window:
        return Window(self.start_time, self.end_time)


# ---------------------------------------------------------------------------
# reasoning
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CandidateLink:
    """A `POSSIBLY_CAUSED` edge proposed by the deterministic linker.

    `heuristics` records *which* rules fired, not just that something did —
    that decomposition is what the confidence model consumes and what lets a
    score be explained rather than asserted.
    """

    cause_id: str
    effect_id: str
    heuristics: tuple[str, ...]
    delta_seconds: int
    score: float = 0.0

    def __post_init__(self) -> None:
        if self.cause_id == self.effect_id:
            raise ValueError(f"event {self.cause_id} cannot cause itself")
        if not self.heuristics:
            raise ValueError(
                f"candidate {self.cause_id}->{self.effect_id} has no heuristics; "
                "an edge nothing voted for should not exist"
            )
        unknown = set(self.heuristics) - HEURISTIC_NAMES
        if unknown:
            raise ValueError(f"unknown heuristics {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A competing explanation, ranked by the Analyst and scored by code.

    `confidence` is never chosen by the model (invariant 7) — `confidence.py`
    computes it from which signals fired.
    """

    id: str
    rank: int
    statement: str
    supporting_event_ids: tuple[str, ...]
    #: Required, may be empty, never unset. "What argues against this?" is the
    #: question that catches confident nonsense, so the field is mandatory.
    contradicting_event_ids: tuple[str, ...]
    confidence: float
    band: Band

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence {self.confidence} outside [0, 1]")
        if self.rank < 1:
            raise ValueError(f"rank must be >= 1, got {self.rank}")
        overlap = set(self.supporting_event_ids) & set(self.contradicting_event_ids)
        if overlap:
            raise ValueError(
                f"events cannot both support and contradict {self.id}: "
                f"{sorted(overlap)}"
            )


@dataclass(frozen=True, slots=True)
class Chain:
    """A candidate causal chain as ranked by the graph, fed to the Analyst.

    `breadth` is how many *other* effects the same cause is linked to — a cause
    that explains several observed effects is stronger than one explaining a
    single symptom.
    """

    cause_id: str
    cause_summary: str
    effect_id: str
    effect_summary: str
    heuristics: tuple[str, ...]
    delta_seconds: int
    breadth: int
    effect_types: tuple[str, ...]

    @property
    def heuristic_count(self) -> int:
        return len(self.heuristics)


@dataclass(frozen=True, slots=True)
class CorrectiveActionStatus:
    description: str
    status: str

    @property
    def is_open(self) -> bool:
        return self.status.lower() not in {"done", "completed", "closed"}


@dataclass(frozen=True, slots=True)
class SimilarIncident:
    """A structurally similar past incident, with the status of its fixes.

    The open corrective actions are the payoff — "third time; the March fix is
    still open" is a sentence no other part of the system can produce.
    """

    incident_id: str
    title: str
    end_time: datetime | None
    shared_components: tuple[str, ...]
    overlap: float
    corrective_actions: tuple[CorrectiveActionStatus, ...] = ()


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
class ComplaintKind(StrEnum):
    MISSING_CITATION = "missing_citation"
    HALLUCINATED_ID = "hallucinated_id"
    TIMESTAMP_MISMATCH = "timestamp_mismatch"
    COVERAGE_BELOW_THRESHOLD = "coverage_below_threshold"


@dataclass(frozen=True, slots=True)
class CitationCheck:
    """The graph's answer about one cited ID.

    `valid` means "this node exists *and* belongs to this incident" — the two
    are checked together, because citing a real node from another incident is
    still a fabrication.
    """

    cited_id: str
    valid: bool
    actual_timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class Complaint:
    """One specific, actionable defect. Fed back to the Writer verbatim —
    "sentence 7 has no citation" is repairable, "improve citations" is not."""

    kind: ComplaintKind
    detail: str
    sentence_index: int | None = None
    sentence: str | None = None
    cited_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The verification plane's verdict — structured, never a bare boolean.

    A boolean can only fail a document; this can tell the Writer what to fix.
    """

    incident_id: str
    factual_sentences: int
    cited_sentences: int
    citations_total: int
    citations_hallucinated: int
    coverage_threshold: float
    complaints: tuple[Complaint, ...] = ()

    @property
    def coverage(self) -> float:
        """Cited ÷ factual. A document with no factual sentences is vacuously
        covered — it makes no claims, so it cannot make an uncited one."""
        if self.factual_sentences == 0:
            return 1.0
        return self.cited_sentences / self.factual_sentences

    @property
    def hallucination_rate(self) -> float:
        if self.citations_total == 0:
            return 0.0
        return self.citations_hallucinated / self.citations_total

    @property
    def timestamp_mismatches(self) -> int:
        return sum(
            1
            for complaint in self.complaints
            if complaint.kind is ComplaintKind.TIMESTAMP_MISMATCH
        )

    @property
    def passed(self) -> bool:
        """Invariant 3: a single hallucinated citation fails the document
        outright, regardless of coverage. It is not a quality score to trade
        off — it is the guarantee the architecture exists to provide.

        A timestamp mismatch is equally disqualifying. A citation naming the
        right event at the wrong time is worse than an uncited sentence,
        because it reads as checked — so it fails the document rather than
        merely being noted in the complaints.
        """
        return (
            self.citations_hallucinated == 0
            and self.timestamp_mismatches == 0
            and self.coverage >= self.coverage_threshold
        )


@dataclass(frozen=True, slots=True)
class SourceFailure:
    """A collector that errored. The run continues with fewer sources and the
    confidence denominator shrinks accordingly — a missing source lowers
    certainty, it does not invent it."""

    source: str
    error: str


@dataclass(frozen=True, slots=True)
class RunReport:
    """What one pipeline run did. Written to disk per run; Phase 2's Grafana
    dashboard reads these."""

    incident_id: str
    started_at: datetime
    finished_at: datetime
    sources_used: tuple[str, ...]
    sources_failed: tuple[SourceFailure, ...]
    event_count: int
    candidate_count: int
    hypothesis_count: int
    retry_count: int
    token_usage: Mapping[str, int]
    validation: ValidationReport | None

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "RunReport.started_at")
        _require_aware(self.finished_at, "RunReport.finished_at")

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def succeeded(self) -> bool:
        return self.validation is not None and self.validation.passed


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------
class PipelineState(TypedDict, total=False):
    """State threaded through the LangGraph DAG (docs/01).

    `total=False` because nodes return partial updates — LangGraph merges each
    node's returned keys into the accumulated state.
    """

    incident_id: str
    incident: Incident
    window: tuple[datetime, datetime]
    events: Sequence[Event]
    graph_ready: bool
    candidates: Sequence[CandidateLink]
    hypotheses: Sequence[Hypothesis]
    similar_incidents: Sequence[SimilarIncident]
    draft_md: str
    #: The assembled document — draft plus the code-rendered header and
    #: timeline. This, not `draft_md`, is what gets validated and published:
    #: nothing should ship that the verification plane has not seen.
    document_md: str
    validation: ValidationReport | None
    retry_count: int
    token_usage: Mapping[str, int]
    #: Collectors that worked, and the ones that did not. Drives the
    #: availability-aware denominator and the postmortem header.
    sources_used: Sequence[str]
    sources_failed: Sequence[SourceFailure]
