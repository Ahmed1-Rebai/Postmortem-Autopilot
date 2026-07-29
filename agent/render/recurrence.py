"""The `## Similar Past Incidents` section, rendered from the graph by code.

Not written by the Writer, on purpose. Whether this incident has happened
before and whether the fix stuck are structural facts read straight off
`SimilarIncident.corrective_actions` — asking a model to phrase "the March fix
is still open" is asking it to source a fact, which invariant 1 forbids. Code
renders it; the Writer never sees it and is never asked to.

Rendered as a table rather than prose for a second reason: the validator's
classifier already skips table rows (they belong to the code-rendered
Timeline), so this section needs no new exemption and cannot be mistaken for
an uncited claim the model made.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from agent.state import CorrectiveActionStatus, SimilarIncident

_HEADING: Final[str] = "## Similar Past Incidents"


def render_similar_incidents(similar: Sequence[SimilarIncident]) -> str:
    """The section, or `""` if nothing matched — it is omitted entirely rather
    than printed as an empty table, since "no recurrence" is the common case
    and shouldn't clutter every document with a heading over nothing."""
    if not similar:
        return ""

    lines = [_HEADING, ""]
    open_total = sum(
        1
        for incident in similar
        for action in incident.corrective_actions
        if action.is_open
    )
    if open_total:
        # The money feature (docs/00): "third time; the March fix is still
        # open." A blockquote, like the header's banners — validator-exempt
        # by the same rule that exempts the tentative-hypothesis banner.
        plural = "action" if open_total == 1 else "actions"
        lines += [
            f"> **{open_total} open corrective {plural} from a past matching "
            "incident.** See below.",
            "",
        ]

    lines += [
        "| Past Incident | Ended | Overlap | Open Corrective Actions |",
        "|---|---|---|---|",
    ]
    for incident in similar:
        ended = f"{incident.end_time:%Y-%m-%d}" if incident.end_time else "unknown"
        lines.append(
            f"| `{incident.incident_id}` — {_escape(incident.title)} "
            f"| {ended} | {incident.overlap:.0%} "
            f"| {_render_actions(incident.corrective_actions)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_actions(actions: Sequence[CorrectiveActionStatus]) -> str:
    open_actions = [action for action in actions if action.is_open]
    if not open_actions:
        return "none open" if actions else "—"
    return "; ".join(_escape(action.description) for action in open_actions)


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()
