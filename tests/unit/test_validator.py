"""The validator, fed hand-written bad documents.

This is the safety net, so it is tested like one: every defect it exists to
catch has a document written to contain that defect and nothing else. If any of
these ever pass, the project's central claim is false and nothing else
compensates (trap 4 in TODO.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from agent.nodes.validator import (
    Citation,
    extract_citations,
    is_factual,
    revise_until_valid,
    split_sentences,
    validate,
)
from agent.state import CitationCheck, ComplaintKind, ValidationReport

REAL_A = "a3f9c1e2b4d6f001"
REAL_B = "52fd7fa8ac6b0fcb"
FAKE = "9f2b000000000000"
OTHER_INCIDENT = "deadbeefdeadbeef"

TS_A = datetime(2026, 3, 12, 14, 0, 40, tzinfo=UTC)
TS_B = datetime(2026, 3, 12, 14, 1, 12, tzinfo=UTC)


def resolver(ids: Sequence[str]) -> dict[str, CitationCheck]:
    """A graph containing exactly two events, both in this incident.

    `OTHER_INCIDENT` is a real node that belongs to a different incident — the
    resolver reports it invalid, because existence alone is not enough.
    """
    known = {REAL_A: TS_A, REAL_B: TS_B}
    return {
        cid: CitationCheck(
            cited_id=cid, valid=cid in known, actual_timestamp=known.get(cid)
        )
        for cid in ids
    }


def check(document: str, threshold: float = 0.95) -> ValidationReport:
    return validate(
        incident_id="INC-0001",
        document=document,
        resolve=resolver,
        coverage_threshold=threshold,
    )


def kinds(report: ValidationReport) -> set[ComplaintKind]:
    return {complaint.kind for complaint in report.complaints}


GOOD = f"""
## Summary

The checkout service began returning errors at 14:00:40 UTC [src:{REAL_A}].
The connection pool was exhausted [src:{REAL_B}].

## Corrective Actions

- Add a canary deploy step.

## Open Questions

- Was the change reviewed?
"""


# ---------------------------------------------------------------------------
# the good document must pass, or nothing below means anything
# ---------------------------------------------------------------------------
def test_a_well_cited_document_passes():
    report = check(GOOD)
    assert report.passed
    assert report.coverage == pytest.approx(1.0)
    assert report.citations_hallucinated == 0
    assert report.complaints == ()


# ---------------------------------------------------------------------------
# defect 1: missing citation
# ---------------------------------------------------------------------------
def test_uncited_factual_sentence_is_caught():
    document = f"""
## Summary

The checkout service began returning errors [src:{REAL_A}].
The database was also under heavy load from a batch job.
"""
    report = check(document)
    assert not report.passed
    assert ComplaintKind.MISSING_CITATION in kinds(report)
    complaint = next(
        c for c in report.complaints if c.kind is ComplaintKind.MISSING_CITATION
    )
    assert complaint.sentence is not None
    assert "batch job" in complaint.sentence
    assert complaint.sentence_index is not None


def test_complaints_identify_the_specific_sentence():
    """ "Sentence 4 has no citation" is repairable; "improve citations" is not."""
    document = f"""
## Summary

First sentence is fine [src:{REAL_A}].
Second sentence is fine [src:{REAL_B}].
Third sentence asserts something with no support at all.
"""
    report = check(document)
    complaint = next(
        c for c in report.complaints if c.kind is ComplaintKind.MISSING_CITATION
    )
    assert complaint.sentence_index == 3


# ---------------------------------------------------------------------------
# defect 2: fabricated ID — invariant 3, a hard failure
# ---------------------------------------------------------------------------
def test_fabricated_id_is_a_hallucination():
    document = f"""
## Summary

The service failed because of a configuration change [src:{FAKE}].
"""
    report = check(document)
    assert not report.passed
    assert report.citations_hallucinated == 1
    assert ComplaintKind.HALLUCINATED_ID in kinds(report)


def test_one_hallucination_fails_a_document_with_perfect_coverage():
    """Invariant 3: hallucinated citations are not traded against coverage.

    Every sentence here carries a *valid* citation, so coverage is 1.00 — but
    one sentence also carries a fabricated one. Coverage cannot buy that off.
    """
    document = f"""
## Summary

The service failed [src:{REAL_A}].
The pool was exhausted [src:{REAL_B}] [src:{FAKE}].
"""
    report = check(document)
    assert report.coverage == pytest.approx(1.0), "every sentence is cited"
    assert not report.passed, "but one citation is fabricated"
    assert report.citations_hallucinated == 1
    assert report.hallucination_rate > 0


def test_a_real_id_from_another_incident_is_still_a_fabrication():
    """Existence is not enough — the check is existence *and* membership."""
    document = f"""
## Summary

The service failed because of an earlier change [src:{OTHER_INCIDENT}].
"""
    report = check(document)
    assert report.citations_hallucinated == 1
    assert not report.passed


def test_a_sentence_cited_only_by_a_fake_id_also_counts_as_uncited():
    """Otherwise a fabricated ID would *raise* coverage."""
    document = f"""
## Summary

The service failed [src:{FAKE}].
"""
    report = check(document)
    assert report.cited_sentences == 0
    assert report.coverage == pytest.approx(0.0)
    assert kinds(report) >= {
        ComplaintKind.HALLUCINATED_ID,
        ComplaintKind.MISSING_CITATION,
    }


# ---------------------------------------------------------------------------
# defect 3: wrong timestamp
# ---------------------------------------------------------------------------
def test_cited_timestamp_that_disagrees_with_the_node_is_caught():
    """A citation naming the right event at the wrong time reads as checked."""
    document = f"""
## Summary

The service began failing at 09:15:00 UTC [src:{REAL_A}, 2026-03-12T09:15:00Z].
"""
    report = check(document)
    assert ComplaintKind.TIMESTAMP_MISMATCH in kinds(report)
    assert not report.passed


def test_matching_timestamp_is_accepted():
    document = f"""
## Summary

The service began failing at 14:00:40 UTC [src:{REAL_A}, 2026-03-12T14:00:40Z].
"""
    report = check(document)
    assert ComplaintKind.TIMESTAMP_MISMATCH not in kinds(report)
    assert report.passed


def test_unparseable_cited_timestamp_is_reported_not_ignored():
    document = f"""
## Summary

The service began failing last Tuesday [src:{REAL_A}, last Tuesday].
"""
    report = check(document)
    assert ComplaintKind.TIMESTAMP_MISMATCH in kinds(report)


def test_a_citation_without_a_timestamp_is_fine():
    """The timestamp is optional; only a *wrong* one is a defect."""
    report = check(f"## Summary\n\nThe service failed [src:{REAL_A}].\n")
    assert report.passed


# ---------------------------------------------------------------------------
# defect 4: coverage drift
# ---------------------------------------------------------------------------
def test_coverage_below_threshold_fails_even_with_no_other_defect():
    document = f"""
## Summary

The service failed [src:{REAL_A}].
The pool was exhausted [src:{REAL_B}].
An uncited claim about the database appears here.
Another uncited claim about the network appears here.
"""
    report = check(document)
    assert report.coverage == pytest.approx(0.5)
    assert ComplaintKind.COVERAGE_BELOW_THRESHOLD in kinds(report)
    assert not report.passed


def test_threshold_is_configurable_and_respected():
    document = f"""
## Summary

The service failed [src:{REAL_A}].
An uncited claim about the database appears here.
"""
    assert not check(document, threshold=0.95).passed
    assert check(document, threshold=0.5).passed


# ---------------------------------------------------------------------------
# the classifier — deliberately dumb, so its edges are readable
# ---------------------------------------------------------------------------
def test_corrective_actions_need_no_citations():
    """An action item is a proposal about the future; no event could cite it."""
    document = f"""
## Summary

The service failed [src:{REAL_A}].

## Corrective Actions

- Add a canary deploy step before full rollout.
- Restore the DB_POOL_MAX setting with an explicit default.
"""
    assert check(document).passed


def test_open_questions_need_no_citations():
    document = f"""
## Summary

The service failed [src:{REAL_A}].

## Open Questions

- Why was the setting removed?
- Nobody has confirmed whether the change was reviewed.
"""
    assert check(document).passed


def test_contributing_factors_do_need_citations():
    """Not exempt: these are claims about what happened, not proposals."""
    document = f"""
## Summary

The service failed [src:{REAL_A}].

## Contributing Factors

- The configuration setting was removed without a replacement default.
"""
    report = check(document)
    assert ComplaintKind.MISSING_CITATION in kinds(report)


def test_headings_tables_and_blockquotes_are_not_sentences():
    """The timeline is code-rendered and the banner is generated; neither is a
    claim the model made."""
    document = f"""
# Postmortem: something

> **Analysis inconclusive.** The leading hypothesis needs verification.

## Timeline

| Time | Type | Event | Source ID |
|---|---|---|---|
| 14:00:40 | commit | removed the setting | `{REAL_A}` |

## Summary

The service failed [src:{REAL_A}].
"""
    report = check(document)
    assert report.factual_sentences == 1
    assert report.passed


def test_code_blocks_are_not_sentences():
    document = f"""
## Summary

The service failed [src:{REAL_A}].

```
This line inside a fence asserts something entirely uncited.
```
"""
    assert check(document).passed


def test_generated_header_metadata_is_not_the_models_prose():
    """`render_document` writes these lines. Demanding citations for the header
    the code itself produced made a clean draft read as 80% covered and sent
    the Writer to fix sentences it never wrote."""
    document = f"""
# Postmortem: checkout 500s

**Incident:** `INC-0001` · **Window:** 2026-03-12T14:00:00Z to 2026-03-12T15:00:00Z

**Evidence sources used:** logs, git, alerts

**Leading hypothesis:** 0.60 (plausible) - the deploy removed the setting

## Summary

The service failed [src:{REAL_A}].
"""
    report = check(document)
    assert report.factual_sentences == 1
    assert report.passed


def test_evidence_absence_statements_are_not_factual():
    """ "No direct evidence of user impact is present" is a statement about the
    evidence, not a claim that needs some."""
    for sentence in (
        "No direct evidence of user-facing impact duration is present.",
        "No evidence links these timeouts to the other events.",
        "Insufficient log data was collected for the payment service.",
    ):
        assert not is_factual(sentence, "impact"), sentence


def test_questions_are_not_factual():
    assert not is_factual("Was the change reviewed before release?", "summary")


def test_meta_sentences_about_the_analysis_are_not_factual():
    """Demanding a citation here would push the Writer to attach an unrelated
    ID — turning a visible gap into an invisible mis-citation."""
    assert not is_factual(
        "This explanation is tentative and needs human verification.", "hypotheses"
    )
    assert not is_factual("No evidence links these timeouts.", "hypotheses")


def test_ordinary_claims_are_factual():
    assert is_factual("The checkout service returned 500s for nine minutes.", "impact")


def test_very_short_fragments_are_not_factual():
    assert not is_factual("Yes.", "summary")
    assert not is_factual("Rank 1.", "hypotheses")


def test_a_sentence_that_is_only_a_citation_is_not_factual():
    assert not is_factual(f"[src:{REAL_A}]", "summary")


def test_version_numbers_do_not_split_sentences():
    """`v2.3.1` must not become three sentences, or coverage is nonsense."""
    sentences = split_sentences(
        f"## Summary\n\nThe rollout of v2.3.1 failed [src:{REAL_A}].\n"
    )
    assert len(sentences) == 1


def test_percentages_do_not_split_sentences():
    sentences = split_sentences(
        f"## Summary\n\nThe error rate hit 5.5% at peak [src:{REAL_A}].\n"
    )
    assert len(sentences) == 1


def test_section_tracking_survives_subsections():
    document = """
## Corrective Actions

### Immediate

- Restore the setting.

## Impact

The service was degraded for nine minutes.
"""
    report = check(document)
    # the Impact claim is factual and uncited; the action items are not
    assert report.factual_sentences == 1


# ---------------------------------------------------------------------------
# citation extraction
# ---------------------------------------------------------------------------
def test_extract_plain_citation():
    assert extract_citations(f"text [src:{REAL_A}].") == (Citation(REAL_A, None),)


def test_extract_citation_with_timestamp():
    citations = extract_citations(f"text [src:{REAL_A}, 2026-03-12T14:00:40Z].")
    assert citations == (Citation(REAL_A, "2026-03-12T14:00:40Z"),)


def test_extract_multiple_citations_in_one_sentence():
    text = f"both things happened [src:{REAL_A}] [src:{REAL_B}]."
    assert len(extract_citations(text)) == 2


def test_extract_tolerates_whitespace():
    assert extract_citations(f"[src: {REAL_A} ]") == (Citation(REAL_A, None),)


def test_text_without_citations_extracts_nothing():
    assert extract_citations("no citations at all here") == ()


# ---------------------------------------------------------------------------
# empty and degenerate documents
# ---------------------------------------------------------------------------
def test_empty_document_is_vacuously_valid():
    """It makes no claims, so it cannot make an uncited one. Whether an empty
    document should ship is the pipeline's decision, not the validator's."""
    report = check("")
    assert report.factual_sentences == 0
    assert report.passed


def test_document_of_only_action_items_is_valid():
    assert check("## Corrective Actions\n\n- Do the thing.\n").passed


def test_no_citations_anywhere_means_no_round_trip(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    def counting_resolver(ids: Sequence[str]) -> dict[str, CitationCheck]:
        calls.append(len(ids))
        return {}

    validate(
        incident_id="INC-0001",
        document="## Corrective Actions\n\n- Do the thing.\n",
        resolve=counting_resolver,
        coverage_threshold=0.95,
    )
    assert calls == []


def test_the_whole_document_resolves_in_one_round_trip():
    """Not one query per citation — the graph is asked once."""
    calls: list[list[str]] = []

    def counting_resolver(ids: Sequence[str]) -> dict[str, CitationCheck]:
        calls.append(list(ids))
        return resolver(ids)

    document = "## Summary\n\n" + "\n".join(
        f"Claim number {i} happened [src:{REAL_A}] [src:{REAL_B}]." for i in range(20)
    )
    validate(
        incident_id="INC-0001",
        document=document,
        resolve=counting_resolver,
        coverage_threshold=0.95,
    )
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted({REAL_A, REAL_B})


# ---------------------------------------------------------------------------
# the retry loop — invariant 8
# ---------------------------------------------------------------------------
BAD = f"## Summary\n\nThe service failed [src:{FAKE}].\n"


def test_a_passing_draft_is_not_rewritten():
    rewrites: list[str] = []

    _, report, attempts = revise_until_valid(
        draft=GOOD,
        validate_draft=check,
        rewrite=lambda d, r: rewrites.append(d) or d,  # type: ignore[func-returns-value]
        max_retries=2,
    )
    assert report.passed
    assert attempts == 0
    assert rewrites == []


def test_a_fixable_draft_is_rewritten_once():
    def rewrite(_draft: str, _report: ValidationReport) -> str:
        return GOOD

    draft, report, attempts = revise_until_valid(
        draft=BAD, validate_draft=check, rewrite=rewrite, max_retries=2
    )
    assert report.passed
    assert attempts == 1
    assert draft == GOOD


def test_retries_are_bounded_and_then_it_fails_loudly():
    """Never a third rewrite, never a lowered threshold, never a pass."""
    attempts_made: list[int] = []

    def stubborn(draft: str, _report: ValidationReport) -> str:
        attempts_made.append(1)
        return BAD

    _, report, attempts = revise_until_valid(
        draft=BAD, validate_draft=check, rewrite=stubborn, max_retries=2
    )
    assert attempts == 2
    assert len(attempts_made) == 2
    assert not report.passed, "a failing draft must never be reported as passing"


def test_the_validator_feedback_reaches_the_rewriter():
    seen: list[ValidationReport] = []

    def rewrite(_draft: str, report: ValidationReport) -> str:
        seen.append(report)
        return GOOD

    revise_until_valid(draft=BAD, validate_draft=check, rewrite=rewrite, max_retries=2)
    assert seen[0].complaints
    assert ComplaintKind.HALLUCINATED_ID in {c.kind for c in seen[0].complaints}


def test_zero_retries_is_allowed():
    _, report, attempts = revise_until_valid(
        draft=BAD,
        validate_draft=check,
        rewrite=lambda d, r: GOOD,
        max_retries=0,
    )
    assert attempts == 0
    assert not report.passed


def test_negative_retries_are_rejected():
    with pytest.raises(ValueError, match="must be >= 0"):
        revise_until_valid(
            draft=GOOD,
            validate_draft=check,
            rewrite=lambda d, r: d,
            max_retries=-1,
        )
