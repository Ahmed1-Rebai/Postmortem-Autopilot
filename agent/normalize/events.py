"""`RawRecord` → `Event`: the contract boundary between sources and the graph.

Two jobs earn their keep here, and both are the difference between a graph that
can be reasoned over and one that can't:

1. **Canonical service names**, so the service-overlap heuristic can fire.
2. **Signature collapsing**, so a few thousand log lines become a handful of
   failure modes. This is what bounds the token cost of an incident: the
   Analyst sees distinct failures, not every line that scrolled past.

Event IDs are content-derived here, and nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from agent.collectors.base import RawRecord
from agent.normalize.services import ServiceCanonicalizer
from agent.normalize.signatures import fingerprint
from agent.state import Event, EventType, make_event_id

#: Event types whose message describes a *symptom* and therefore carries an
#: error signature. A commit message or a chat line has no failure mode to
#: fingerprint, and pretending otherwise would put noise in the graph's
#: signature index and in every incident fingerprint built from it.
SIGNATURE_BEARING: frozenset[EventType] = frozenset(
    {EventType.LOG_ERROR, EventType.ALERT_FIRED}
)


def to_event(record: RawRecord, canonicalizer: ServiceCanonicalizer) -> Event:
    """Normalize one record. No collapsing — see `normalize_records`."""
    signature = None
    if record.type in SIGNATURE_BEARING:
        signature = fingerprint(record.message) or None

    return Event(
        id=make_event_id(record.source, record.native_id),
        type=record.type,
        timestamp=record.timestamp,
        source=record.source,
        summary=record.message.strip(),
        service=canonicalizer.canonical(record.service_hint),
        signature=signature,
        attributes=dict(record.attributes),
        raw=dict(record.raw),
    )


def normalize_records(
    records: Iterable[RawRecord], canonicalizer: ServiceCanonicalizer
) -> list[Event]:
    """Normalize a batch, collapsing repeated log errors into one event each.

    Log errors sharing a `(service, signature)` are one failure mode. The
    collapsed event keeps the **earliest** timestamp — the first occurrence is
    the causally interesting one, and using the last would place the effect
    after events it actually preceded — and a `count` of how many lines it
    stands for.

    Everything else passes through untouched. Two deploys are two deploys even
    if their messages are identical.
    """
    events = [to_event(record, canonicalizer) for record in records]

    collapsible: dict[tuple[str | None, str], list[Event]] = {}
    passthrough: list[Event] = []
    for event in events:
        if event.type is EventType.LOG_ERROR and event.signature:
            collapsible.setdefault((event.service, event.signature), []).append(event)
        else:
            passthrough.append(event)

    collapsed = [_collapse(group) for group in collapsible.values()]
    return sorted(passthrough + collapsed, key=lambda e: (e.timestamp, e.id))


def _collapse(group: Sequence[Event]) -> Event:
    """Fold one `(service, signature)` group into a single event.

    The collapsed event's ID is derived from the signature rather than from any
    one line, so re-collecting the same window produces the same node even if
    the underlying lines arrive in a different order or a different quantity —
    which is what keeps citations stable across reruns.
    """
    if len(group) == 1:
        only = group[0]
        # Still re-key onto the signature, so a second run that sees two
        # occurrences merges onto this same node instead of creating a new one.
        return _rekey(only, occurrences=only.count)

    earliest = min(group, key=lambda e: e.timestamp)
    total = sum(event.count for event in group)
    return _rekey(earliest, occurrences=total)


def _rekey(event: Event, occurrences: int) -> Event:
    assert event.signature is not None
    native_id = f"log:sig:{event.signature}"
    if event.service:
        native_id = f"log:{event.service}:sig:{event.signature}"
    return Event(
        id=make_event_id(event.source, native_id),
        type=event.type,
        timestamp=event.timestamp,
        source=event.source,
        summary=event.summary,
        service=event.service,
        signature=event.signature,
        count=occurrences,
        attributes=dict(event.attributes),
        raw=dict(event.raw),
    )
