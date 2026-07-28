"""The verification plane. This is the project.

Four checks, run by code against the graph:

| Check | Failure mode it catches |
|---|---|
| every factual sentence has a `[src:...]` tag | the model asserting an uncited claim |
| every cited ID exists *in this incident* | fabricated or cross-incident citation |
| the cited timestamp matches the node's | plausible-looking but wrong attribution |
| coverage ≥ threshold | slow drift toward narrative prose |

Two design commitments worth stating plainly:

**Citation validity is resolved against Neo4j, never inferred.** A regex that
checks an ID *looks* like an ID would pass `deadbeefdeadbeef` forever. The
resolver is injected rather than imported so unit tests can hand it a
dictionary, while integration tests hand it the real Cypher — but there is no
mode in which the check is skipped.

**The classifier is deliberately dumb.** A smart one would be a second model
with its own failure modes, unmeasurable and unfixable. This is regexes and a
section allow-list, wrong in ways you can read off the page, and its own
accuracy is a metric in the eval suite.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from agent.state import (
    CitationCheck,
    Complaint,
    ComplaintKind,
    ValidationReport,
)

#: `[src:a3f9c1e2b4d6f001]` or `[src:a3f9c1e2b4d6f001, 2026-03-12T14:02:11Z]`.
CITATION: Final[re.Pattern[str]] = re.compile(
    r"\[src:\s*(?P<id>[0-9a-zA-Z:_-]{4,80}?)\s*(?:,\s*(?P<ts>[^\]]+?))?\s*\]"
)

#: Sections whose sentences are recommendations or questions rather than
#: factual claims. An action item is a proposal about the future; there is no
#: event that could cite it.
EXEMPT_SECTIONS: Final[frozenset[str]] = frozenset(
    {
        "corrective actions",
        "open questions",
        "action items",
        "recommendations",
        "next steps",
        "follow-ups",
    }
)

#: Sentences *about the analysis* rather than about the incident. "This
#: explanation is tentative" is a statement about confidence, and demanding a
#: citation for it would push the Writer to attach an unrelated ID — turning a
#: visible gap into an invisible mis-citation.
_META: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^this (explanation|hypothesis|analysis|conclusion|section|document)\b",
        r"^(no|insufficient|little)\s+(\w+\s+){0,2}(evidence|data|record)\b",
        r"\bneeds? human verification\b",
        r"\b(the )?evidence (is|was) "
        r"(absent|inconclusive|insufficient|not available)\b",
        r"^(disconfirming|supporting) evidence includes\b",
        r"^(treat|consider|review|add|implement|investigate|verify|ensure|"
        r"document|create|update|remove|migrate|adopt|introduce|audit)\b",
    )
)

_HEADING: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM: Final[re.Pattern[str]] = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$")
#: Generated metadata lines — `**Incident:** INC-0001`, `**Evidence sources
#: used:** logs, git`. These come from `render_document`, not from the model.
#: Demanding citations for the header the code itself wrote made a clean draft
#: read as 80% covered and sent the Writer to fix sentences it never wrote.
_METADATA_LINE: Final[re.Pattern[str]] = re.compile(r"^\*\*[^*]+:\*\*")
#: Split on sentence punctuation only when what follows looks like a new
#: sentence. Keeps `v2.3.1` and `82.5%` intact.
_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'`\[(])")
_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]{2,}")

#: A resolver takes cited IDs and answers whether each is a real node in *this*
#: incident. Injected so tests can supply a dict; production supplies Cypher.
Resolver = Callable[[Sequence[str]], dict[str, CitationCheck]]


@dataclass(frozen=True, slots=True)
class Citation:
    event_id: str
    timestamp_text: str | None


@dataclass(frozen=True, slots=True)
class Sentence:
    index: int
    text: str
    section: str
    is_factual: bool
    citations: tuple[Citation, ...]


def extract_citations(text: str) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            event_id=match.group("id"),
            timestamp_text=(match.group("ts") or "").strip() or None,
        )
        for match in CITATION.finditer(text)
    )


def split_sentences(document: str) -> list[Sentence]:
    """Sentences with their section and whether they assert a fact.

    Structural lines are skipped outright: headings name sections, table rows
    belong to the code-rendered timeline, blockquotes are the generated banner,
    and fenced code is not prose. None of them are the model's claims.
    """
    sentences: list[Sentence] = []
    #: A stack, not a single value: `### Immediate` nested under `## Corrective
    #: Actions` must stay exempt. Tracking only the latest heading made every
    #: sub-headed action item a factual claim.
    section_path: list[str] = []
    in_code = False
    index = 0

    for raw in document.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue

        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            # Drop this level and everything deeper, then pad for levels the
            # document skipped — a doc that starts at `##` must still place it
            # at depth 2, or the next `##` inherits its predecessor's path.
            del section_path[level - 1 :]
            section_path.extend([""] * (level - 1 - len(section_path)))
            section_path.append(heading.group(2).strip().lower())
            continue
        section = " > ".join(section_path)
        if (
            stripped.startswith("|")
            or stripped.startswith(">")
            or _METADATA_LINE.match(stripped)
        ):
            continue

        item = _LIST_ITEM.match(line)
        body = item.group(1) if item else stripped
        if not body.strip():
            continue

        for piece in _split_into_sentences(body):
            index += 1
            sentences.append(
                Sentence(
                    index=index,
                    text=piece,
                    section=section,
                    is_factual=is_factual(piece, section),
                    citations=extract_citations(piece),
                )
            )
    return sentences


def is_factual(sentence: str, section: str) -> bool:
    """Whether this sentence asserts a checkable fact about the incident."""
    if _section_is_exempt(section):
        return False
    text = sentence.strip()
    if text.endswith("?"):
        return False
    if len(_WORD.findall(text)) < 3:
        return False
    without_citations = CITATION.sub("", text).strip()
    if len(_WORD.findall(without_citations)) < 3:
        return False
    return not any(pattern.search(without_citations) for pattern in _META)


def validate(
    *,
    incident_id: str,
    document: str,
    resolve: Resolver,
    coverage_threshold: float,
) -> ValidationReport:
    """Check a draft. Returns a structured report, never a bare boolean.

    A boolean can only reject a document; this can tell the Writer what to fix.
    """
    sentences = split_sentences(document)
    factual = [sentence for sentence in sentences if sentence.is_factual]

    all_cited_ids = [
        citation.event_id for sentence in sentences for citation in sentence.citations
    ]
    # One round trip for the whole document, checking existence *and* incident
    # membership together — a real node cited from another incident is still a
    # fabrication.
    resolved = resolve(list(dict.fromkeys(all_cited_ids))) if all_cited_ids else {}

    complaints: list[Complaint] = []
    hallucinated = 0
    cited_sentences = 0

    for sentence in sentences:
        valid_here = 0
        for citation in sentence.citations:
            check = resolved.get(citation.event_id)
            if check is None or not check.valid:
                hallucinated += 1
                complaints.append(
                    Complaint(
                        kind=ComplaintKind.HALLUCINATED_ID,
                        detail=(
                            f"cited {citation.event_id} which is not an event in "
                            f"incident {incident_id}"
                        ),
                        sentence_index=sentence.index,
                        sentence=sentence.text,
                        cited_id=citation.event_id,
                    )
                )
                continue

            valid_here += 1
            mismatch = _timestamp_mismatch(citation, check)
            if mismatch is not None:
                complaints.append(
                    Complaint(
                        kind=ComplaintKind.TIMESTAMP_MISMATCH,
                        detail=mismatch,
                        sentence_index=sentence.index,
                        sentence=sentence.text,
                        cited_id=citation.event_id,
                    )
                )

        if not sentence.is_factual:
            continue
        if valid_here:
            cited_sentences += 1
        else:
            complaints.append(
                Complaint(
                    kind=ComplaintKind.MISSING_CITATION,
                    detail=(
                        "factual sentence has no citation that resolves to an "
                        "event in this incident"
                    ),
                    sentence_index=sentence.index,
                    sentence=sentence.text,
                )
            )

    report = ValidationReport(
        incident_id=incident_id,
        factual_sentences=len(factual),
        cited_sentences=cited_sentences,
        citations_total=len(all_cited_ids),
        citations_hallucinated=hallucinated,
        coverage_threshold=coverage_threshold,
        complaints=tuple(complaints),
    )

    if report.coverage < coverage_threshold:
        report = ValidationReport(
            incident_id=report.incident_id,
            factual_sentences=report.factual_sentences,
            cited_sentences=report.cited_sentences,
            citations_total=report.citations_total,
            citations_hallucinated=report.citations_hallucinated,
            coverage_threshold=report.coverage_threshold,
            complaints=(
                *report.complaints,
                Complaint(
                    kind=ComplaintKind.COVERAGE_BELOW_THRESHOLD,
                    detail=(
                        f"citation coverage {report.coverage:.2%} is below the "
                        f"{coverage_threshold:.2%} threshold "
                        f"({report.cited_sentences}/{report.factual_sentences} "
                        "factual sentences cited)"
                    ),
                ),
            ),
        )
    return report


def revise_until_valid(
    *,
    draft: str,
    validate_draft: Callable[[str], ValidationReport],
    rewrite: Callable[[str, ValidationReport], str],
    max_retries: int,
) -> tuple[str, ValidationReport, int]:
    """Validate, and rewrite with the validator's complaints, up to a bound.

    Invariant 8: the threshold is never lowered, the retry count is never
    exceeded, and a failing draft is never published. The caller writes the
    draft and the report to disk and exits non-zero — a pipeline that quietly
    lowers its own standards is worse than one that stops.

    `rewrite` is injected rather than imported so this loop can be tested
    without a model, which is the whole reason the mock provider exists.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    report = validate_draft(draft)
    attempts = 0
    while not report.passed and attempts < max_retries:
        attempts += 1
        draft = rewrite(draft, report)
        report = validate_draft(draft)
    return draft, report, attempts


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _section_is_exempt(section: str) -> bool:
    """Exempt if *any* ancestor heading is exempt.

    `section` is the heading path (`corrective actions > immediate`), so a
    subsection cannot escape the exemption its parent granted.
    """
    normalized = section.strip().lower()
    return any(exempt in normalized for exempt in EXEMPT_SECTIONS)


def _split_into_sentences(body: str) -> list[str]:
    return [piece.strip() for piece in _SENTENCE_SPLIT.split(body) if piece.strip()]


def _timestamp_mismatch(citation: Citation, check: CitationCheck) -> str | None:
    """Compare a cited timestamp to the node's, if one was given.

    A citation naming the right event at the wrong time is worse than an
    uncited sentence: it reads as checked. Unparseable text is reported rather
    than ignored, because "[src:x, last Tuesday]" is not a timestamp.
    """
    if citation.timestamp_text is None:
        return None
    actual = check.actual_timestamp
    if actual is None:
        return None

    cited = _parse_timestamp(citation.timestamp_text)
    if cited is None:
        return (
            f"cited timestamp {citation.timestamp_text!r} for "
            f"{citation.event_id} is not a parseable UTC timestamp"
        )
    if abs((cited - actual).total_seconds()) >= 1.0:
        return (
            f"cited timestamp {citation.timestamp_text} does not match the "
            f"event's actual timestamp {actual:%Y-%m-%dT%H:%M:%SZ}"
        )
    return None


def _parse_timestamp(text: str) -> datetime | None:
    from datetime import UTC

    candidate = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
