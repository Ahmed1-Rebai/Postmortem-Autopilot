"""Recurrence: the code-rendered section and the extraction that feeds it.

Two things get pinned here:

- the section is a table, so it inherits the validator's existing "table rows
  need no citation" rule rather than needing a new exemption
- extraction reuses the validator's own section-exemption logic, so a bullet
  under "Corrective Actions" is recognized the same way whether the question
  is "does this need a [src:...] tag" or "should this become a graph node"
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.nodes.recurrence import extract_corrective_actions
from agent.nodes.validator import validate
from agent.render.recurrence import render_similar_incidents
from agent.state import CitationCheck, CorrectiveActionStatus, SimilarIncident

END = datetime(2026, 3, 12, 15, 0, tzinfo=UTC)


def similar(
    incident_id: str = "INC-0001",
    title: str = "checkout 500s after deploy",
    overlap: float = 0.75,
    actions: tuple[CorrectiveActionStatus, ...] = (),
) -> SimilarIncident:
    return SimilarIncident(
        incident_id=incident_id,
        title=title,
        end_time=END,
        shared_components=("commit|log_error|checkout|pool exhausted",),
        overlap=overlap,
        corrective_actions=actions,
    )


# ---------------------------------------------------------------------------
# render_similar_incidents
# ---------------------------------------------------------------------------
def test_no_matches_renders_nothing():
    """The common case must not clutter every document with an empty section."""
    assert render_similar_incidents([]) == ""


def test_a_match_with_no_corrective_actions_still_renders():
    rendered = render_similar_incidents([similar()])
    assert "## Similar Past Incidents" in rendered
    assert "INC-0001" in rendered
    assert "75%" in rendered
    assert "—" in rendered  # no actions were ever recorded for this incident


def test_an_open_action_triggers_the_money_banner():
    """ "Third time; the March fix is still open" — the feature docs/00 names."""
    action = CorrectiveActionStatus("Add a connection pool size alert", "open")
    rendered = render_similar_incidents([similar(actions=(action,))])
    assert "1 open corrective action" in rendered
    assert "Add a connection pool size alert" in rendered


def test_a_closed_action_does_not_trigger_the_banner():
    action = CorrectiveActionStatus("Add a connection pool size alert", "done")
    rendered = render_similar_incidents([similar(actions=(action,))])
    assert "open corrective action" not in rendered
    assert "none open" in rendered


def test_multiple_open_actions_pluralize_correctly():
    actions = (
        CorrectiveActionStatus("Add pool alert", "open"),
        CorrectiveActionStatus("Add canary rollout", "open"),
    )
    rendered = render_similar_incidents([similar(actions=actions)])
    assert "2 open corrective actions" in rendered


def test_only_open_actions_are_listed_when_mixed():
    actions = (
        CorrectiveActionStatus("Add pool alert", "open"),
        CorrectiveActionStatus("Rotate the on-call doc", "done"),
    )
    rendered = render_similar_incidents([similar(actions=actions)])
    assert "Add pool alert" in rendered
    assert "Rotate the on-call doc" not in rendered


def test_pipe_and_newline_in_titles_do_not_break_the_table():
    """A `|` in an LLM-influenced field would otherwise shift every column."""
    rendered = render_similar_incidents(
        [similar(title="checkout | payment outage\nsecond line")]
    )
    lines = [line for line in rendered.splitlines() if line.startswith("| `INC")]
    assert len(lines) == 1
    assert "\n" not in lines[0]


def test_missing_end_time_is_handled():
    incident = SimilarIncident(
        incident_id="INC-0001",
        title="x",
        end_time=None,
        shared_components=(),
        overlap=0.5,
    )
    assert "unknown" in render_similar_incidents([incident])


def test_the_rendered_table_rows_are_validator_exempt():
    """The section needs no new exemption: table rows are already skipped by
    the citation classifier, so this content is never flagged as an uncited
    claim the model made — because the model never made it."""
    action = CorrectiveActionStatus("Add a connection pool size alert", "open")
    document = (
        "## Summary\n\nThe service failed [src:aaaaaaaaaaaaaaaa].\n\n"
        + render_similar_incidents([similar(actions=(action,))])
    )

    def resolver(ids: object) -> dict[str, CitationCheck]:
        return {
            "aaaaaaaaaaaaaaaa": CitationCheck(
                "aaaaaaaaaaaaaaaa", True, actual_timestamp=END
            )
        }

    report = validate(
        incident_id="INC-0007",
        document=document,
        resolve=resolver,
        coverage_threshold=0.95,
    )
    assert report.passed
    assert report.factual_sentences == 1


# ---------------------------------------------------------------------------
# extract_corrective_actions
# ---------------------------------------------------------------------------
DOC = """
## Summary

The service failed [src:aaaaaaaaaaaaaaaa].

## Corrective Actions

- Add a connection pool size alert.
- Restore the DB_POOL_MAX default explicitly.

## Open Questions

- Was the change reviewed before release?
"""


def test_extracts_only_corrective_action_bullets():
    actions = extract_corrective_actions(DOC)
    assert actions == [
        "Add a connection pool size alert.",
        "Restore the DB_POOL_MAX default explicitly.",
    ]


def test_open_questions_are_not_corrective_actions():
    assert "Was the change reviewed before release?" not in extract_corrective_actions(
        DOC
    )


def test_summary_sentences_are_not_corrective_actions():
    assert not any("service failed" in a for a in extract_corrective_actions(DOC))


def test_a_document_with_no_corrective_actions_section_extracts_nothing():
    document = "## Summary\n\nThe service failed [src:aaaaaaaaaaaaaaaa].\n"
    assert extract_corrective_actions(document) == []


def test_duplicate_bullets_are_deduplicated_in_order():
    document = (
        "## Corrective Actions\n\n"
        "- Add a connection pool size alert.\n"
        "- Restore the default.\n"
        "- Add a connection pool size alert.\n"
    )
    assert extract_corrective_actions(document) == [
        "Add a connection pool size alert.",
        "Restore the default.",
    ]


def test_a_subsection_under_corrective_actions_still_counts():
    """Nested headings inherit their parent's section — the same rule the
    validator uses to keep sub-headed action items citation-exempt."""
    document = (
        "## Corrective Actions\n\n### Immediate\n\n- Add a connection pool alert.\n"
    )
    assert extract_corrective_actions(document) == ["Add a connection pool alert."]
