"""Resolving `expected.yaml`'s human-readable pseudo-IDs against the real,
content-derived `Event` IDs a run actually produces.

A golden incident's label can't reference `agent.state.make_event_id`'s
`sha256(source:native_id)[:16]` output directly — that hash isn't knowable
until a run happens. So `expected.yaml` uses readable pseudo-IDs instead,
each prefixed by how to find the real event:

- `commit:<subject>`   — a `COMMIT` event whose `summary` (the commit
  subject line, `agent.collectors.git`) matches, case-insensitively.
- `alert:<rule_name>`  — an `ALERT_FIRED` event whose
  `attributes["rule_name"]` (`agent.collectors.alerts`) matches exactly.
- `log:sig:<pattern>`  — a `LOG_ERROR` event whose `signature`
  (`agent.normalize.signatures.fingerprint`, already lowercased and
  number-masked to `<num>`) matches exactly.

This is this project's own reading of docs/07-evaluation.md's illustrative
label strings, not a documented contract — docs/07 shows the format but
never states the resolution algorithm.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent.state import Event, EventType

_COMMIT_PREFIX = "commit:"
_ALERT_PREFIX = "alert:"
_LOG_SIG_PREFIX = "log:sig:"


class UnresolvedLabelError(ValueError):
    """A pseudo-ID matched no collected event, or has no recognized prefix.

    This is a fixture bug, not a model failure — surfaced loudly rather than
    silently scored as a miss, which would make a broken `expected.yaml`
    look like a model regression.
    """


def resolve_label(pseudo_id: str, events: Sequence[Event]) -> list[Event]:
    """All events a pseudo-ID refers to. Raises if it refers to none."""
    if pseudo_id.startswith(_LOG_SIG_PREFIX):
        pattern = pseudo_id[len(_LOG_SIG_PREFIX) :]
        matches = [
            e
            for e in events
            if e.type is EventType.LOG_ERROR and e.signature == pattern
        ]
    elif pseudo_id.startswith(_COMMIT_PREFIX):
        subject = pseudo_id[len(_COMMIT_PREFIX) :].strip().lower()
        matches = [
            e
            for e in events
            if e.type is EventType.COMMIT and e.summary.strip().lower() == subject
        ]
    elif pseudo_id.startswith(_ALERT_PREFIX):
        rule_name = pseudo_id[len(_ALERT_PREFIX) :].strip()
        matches = [
            e
            for e in events
            if e.type is EventType.ALERT_FIRED
            and e.attributes.get("rule_name") == rule_name
        ]
    else:
        raise UnresolvedLabelError(
            f"{pseudo_id!r} has no recognized prefix "
            f"({_COMMIT_PREFIX!r}/{_ALERT_PREFIX!r}/{_LOG_SIG_PREFIX!r})"
        )

    if not matches:
        raise UnresolvedLabelError(
            f"{pseudo_id!r} resolved to no collected events — check the fixture, "
            "not the model"
        )
    return matches


def resolve_labels(pseudo_ids: Sequence[str], events: Sequence[Event]) -> list[Event]:
    """`resolve_label` over a list, flattened. Raises on the first unresolved one."""
    resolved: list[Event] = []
    for pseudo_id in pseudo_ids:
        resolved.extend(resolve_label(pseudo_id, events))
    return resolved


def resolved_ids(pseudo_ids: Sequence[str], events: Sequence[Event]) -> frozenset[str]:
    """The real event IDs a set of pseudo-IDs resolves to, deduplicated."""
    return frozenset(event.id for event in resolve_labels(pseudo_ids, events))
