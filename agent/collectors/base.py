"""The collector contract.

A collector's only job is to get records out of a source and into one shape.
It does not canonicalize service names, compute signatures, or assign event
IDs — that is the normalizer's job, and keeping the split means adding a new
evidence source is a new `Collector` plus a fixture, and nothing else changes.

Collectors are pure and offline-testable. A failing collector **degrades** the
run rather than aborting it: the pipeline continues with fewer sources, the
gap is recorded in the `RunReport`, and the confidence model drops that signal
from its denominator rather than penalizing the score for it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from agent.state import AttributeValue, EventType, Window


class CollectorError(RuntimeError):
    """A source could not be read. Caught by the pipeline, recorded as a gap."""


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One record as the source described it, before normalization.

    `native_id` is the source's own stable identifier — a commit SHA, an alert
    name plus start time, a collapsed log signature. It is half of the
    content-derived event ID, so it must be stable across re-collection: if it
    changes between runs, the same evidence lands on two nodes and every
    corroboration count that touches it is wrong.
    """

    source: str
    native_id: str
    type: EventType
    timestamp: datetime
    message: str
    #: The service as this source names it — not yet canonical.
    service_hint: str | None = None
    #: Subtype properties (commit SHA, alert rule, log level). Passed through
    #: to `Event.attributes` and stored as graph properties.
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.native_id:
            raise ValueError(f"{self.source} produced a record with no native_id")
        if self.timestamp.tzinfo is None:
            raise ValueError(
                f"{self.source}:{self.native_id} has a naive timestamp; "
                "collectors must resolve the source's timezone"
            )


@runtime_checkable
class Collector(Protocol):
    """One evidence source."""

    name: str

    def collect(self, window: Window, service: str | None) -> Sequence[RawRecord]:
        """Records inside `window`, optionally narrowed to one service.

        Raises `CollectorError` if the source is unreachable or malformed. It
        must not return partial results silently — a half-read source looks
        exactly like a quiet incident, and the confidence model would treat the
        missing evidence as evidence of absence.
        """
        ...
