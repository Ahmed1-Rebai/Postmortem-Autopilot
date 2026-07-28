"""The reasoning plane.

LLM nodes are evaluated through the eval suite, not by asserting on prose. What
*is* asserted here is the code around the model — the part that makes the
prompt's instructions unnecessary:

- an ID the model was not given never reaches the graph (invariant 1)
- the model never sets confidence (invariant 7)
- the Timeline is code-rendered even if the model writes its own
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.config import ConfidenceConfig, LLMConfig
from agent.llm import (
    LLMError,
    LLMResponse,
    MockProvider,
    RetryingProvider,
    TransientLLMError,
    build_provider,
    extract_json,
)
from agent.nodes.analyst import analyze
from agent.nodes.writer import write_draft
from agent.prompts import PromptError
from agent.prompts import load as load_prompt
from agent.render.document import render_document
from agent.render.timeline import (
    render_chains_for_model,
    render_evidence_for_model,
    render_timeline,
)
from agent.state import (
    Chain,
    Complaint,
    ComplaintKind,
    Event,
    EventType,
    Hypothesis,
    Incident,
    ValidationReport,
)

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)
SOURCES = ["logs", "git", "alerts"]


@pytest.fixture
def confidence_config() -> ConfidenceConfig:
    return ConfidenceConfig(
        weights={
            "change_path_overlap": 0.30,
            "log_signature_match": 0.25,
            "metric_correlation": 0.20,
            "temporal_proximity": 0.15,
            "human_confirmation": 0.10,
        },
        contradiction_per_event=0.15,
        contradiction_cap=0.40,
        band_likely=0.75,
        band_plausible=0.40,
    )


def event(event_id: str, kind: EventType, offset: int, **kw: object) -> Event:
    return Event(
        id=event_id,
        type=kind,
        timestamp=START + timedelta(seconds=offset),
        source=str(kw.get("source", "test")),
        summary=str(kw.get("summary", event_id)),
        service=kw.get("service", "checkout"),  # type: ignore[arg-type]
        signature=kw.get("signature"),  # type: ignore[arg-type]
    )


@pytest.fixture
def events() -> list[Event]:
    return [
        event("aaaaaaaaaaaaaaaa", EventType.COMMIT, 0, summary="remove DB_POOL_MAX"),
        event(
            "bbbbbbbbbbbbbbbb",
            EventType.LOG_ERROR,
            60,
            summary="pool exhausted",
            signature="pool exhausted",
        ),
        event("cccccccccccccccc", EventType.ALERT_FIRED, 120, summary="HighErrorRate"),
    ]


@pytest.fixture
def chains() -> list[Chain]:
    return [
        Chain(
            cause_id="aaaaaaaaaaaaaaaa",
            cause_summary="remove DB_POOL_MAX",
            effect_id="bbbbbbbbbbbbbbbb",
            effect_summary="pool exhausted",
            heuristics=("temporal_proximity", "change_path_overlap"),
            delta_seconds=60,
            breadth=2,
            effect_types=("log_error",),
        )
    ]


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["analyst", "writer", "repair"])
def test_prompts_load_from_disk(name: str):
    """Prompts are files, never inline strings — in k3s they are a ConfigMap,
    so a prompt change must not require a rebuild."""
    assert len(load_prompt(name)) > 200


def test_missing_prompt_fails_loudly(tmp_path: Path):
    with pytest.raises(PromptError, match="not found"):
        load_prompt("nonexistent", tmp_path)


def test_analyst_prompt_forbids_choosing_confidence():
    assert "not yours to choose" in load_prompt("analyst")


def test_writer_prompt_forbids_writing_a_timeline():
    assert "Do **not** write a Timeline section" in load_prompt("writer")


# ---------------------------------------------------------------------------
# JSON extraction — models wrap their output in all sorts of things
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        '{"hypotheses": []}',
        '```json\n{"hypotheses": []}\n```',
        '```\n{"hypotheses": []}\n```',
        'Here is the analysis:\n{"hypotheses": []}',
        'Thinking about it...\n\n{"hypotheses": []}\n\nHope that helps!',
    ],
)
def test_extract_json_tolerates_wrappers(text: str):
    assert extract_json(text) == {"hypotheses": []}


def test_extract_json_fails_loudly_on_prose():
    with pytest.raises(LLMError, match="no parseable JSON"):
        extract_json("I cannot help with that request.")


# ---------------------------------------------------------------------------
# retry with backoff — the docs/01 failure table
# ---------------------------------------------------------------------------
@dataclass
class FlakyProvider:
    """Fails transiently `fail_times`, then succeeds."""

    fail_times: int
    name: str = "flaky"
    calls: int = 0

    def complete(self, **_: object) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TransientLLMError("rate limited by upstream")
        return LLMResponse(text="ok", model="flaky")


@dataclass
class BrokenProvider:
    """Fails in a way retrying cannot fix."""

    name: str = "broken"
    calls: int = 0

    def complete(self, **_: object) -> LLMResponse:
        self.calls += 1
        raise LLMError("response hit max_tokens and is truncated")


def call(provider: object) -> LLMResponse:
    return provider.complete(  # type: ignore[attr-defined]
        system="s", user="u", model="m", max_tokens=100
    )


def test_a_transient_failure_is_retried():
    inner = FlakyProvider(fail_times=2)
    provider = RetryingProvider(inner, sleep=lambda _: None)
    assert call(provider).text == "ok"
    assert inner.calls == 3


def test_retries_are_bounded_at_three_attempts():
    inner = FlakyProvider(fail_times=99)
    provider = RetryingProvider(inner, sleep=lambda _: None)
    with pytest.raises(LLMError, match="after 3 attempts"):
        call(provider)
    assert inner.calls == 3


def test_backoff_is_exponential():
    delays: list[float] = []
    provider = RetryingProvider(
        FlakyProvider(fail_times=99), base_delay_seconds=2.0, sleep=delays.append
    )
    with pytest.raises(LLMError):
        call(provider)
    assert delays == [2.0, 4.0], "one sleep between attempts, doubling"


def test_a_non_transient_failure_is_not_retried():
    """Truncation and malformed JSON fail identically on the next attempt, so
    retrying burns the budget and delays the real diagnosis."""
    inner = BrokenProvider()
    provider = RetryingProvider(inner, sleep=lambda _: None)
    with pytest.raises(LLMError, match="truncated"):
        call(provider)
    assert inner.calls == 1


def test_the_wrapper_keeps_the_inner_providers_name():
    assert RetryingProvider(FlakyProvider(fail_times=0)).name == "flaky"


def test_the_mock_provider_is_not_wrapped():
    """No network, so there is nothing to back off from — and a test that
    accidentally slept would be a slow test for no reason."""
    config = LLMConfig(
        provider="mock", analyst_model="m", writer_model="m", api_key=None
    )
    assert isinstance(build_provider(config), MockProvider)


# ---------------------------------------------------------------------------
# the analyst — invariant 1 and invariant 7 enforced by code
# ---------------------------------------------------------------------------
def test_fabricated_ids_never_reach_the_graph(
    events: list[Event], chains: list[Chain], confidence_config: ConfidenceConfig
):
    """The prompt asks the model not to invent IDs. This is what makes asking
    unnecessary."""
    provider = MockProvider(
        scripted=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "statement": "the deploy did it",
                            "supporting_event_ids": [
                                "aaaaaaaaaaaaaaaa",
                                "9f2b000000000000",  # never existed
                            ],
                            "contradicting_event_ids": ["deadbeefdeadbeef"],
                        }
                    ]
                }
            )
        ]
    )
    result = analyze(
        incident_id="INC-0001",
        events=events,
        chains=chains,
        provider=provider,
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    assert result.hypotheses[0].supporting_event_ids == ("aaaaaaaaaaaaaaaa",)
    assert result.hypotheses[0].contradicting_event_ids == ()
    assert result.dropped_ids == ("9f2b000000000000", "deadbeefdeadbeef")


def test_a_hypothesis_with_only_fabricated_support_is_discarded(
    events: list[Event], chains: list[Chain], confidence_config: ConfidenceConfig
):
    provider = MockProvider(
        scripted=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "statement": "entirely invented",
                            "supporting_event_ids": ["9f2b000000000000"],
                            "contradicting_event_ids": [],
                        }
                    ]
                }
            )
        ]
    )
    result = analyze(
        incident_id="INC-0001",
        events=events,
        chains=chains,
        provider=provider,
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    assert result.hypotheses == ()


def test_model_supplied_confidence_is_ignored(
    events: list[Event], chains: list[Chain], confidence_config: ConfidenceConfig
):
    """Invariant 7: the model may not choose the number, even if it offers one."""
    provider = MockProvider(
        scripted=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "statement": "the deploy did it",
                            "supporting_event_ids": [
                                "aaaaaaaaaaaaaaaa",
                                "bbbbbbbbbbbbbbbb",
                            ],
                            "contradicting_event_ids": [],
                            "confidence": 0.99,
                            "band": "likely",
                        }
                    ]
                }
            )
        ]
    )
    result = analyze(
        incident_id="INC-0001",
        events=events,
        chains=chains,
        provider=provider,
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    # change_path + temporal + log_signature = 0.70 / 0.90
    assert result.hypotheses[0].confidence == pytest.approx(0.7778, abs=1e-3)


def test_hypotheses_are_reordered_by_computed_confidence(
    events: list[Event], chains: list[Chain], confidence_config: ConfidenceConfig
):
    """The model proposes an order; the number decides it."""
    provider = MockProvider(
        scripted=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "statement": "weak one the model put first",
                            "supporting_event_ids": ["cccccccccccccccc"],
                            "contradicting_event_ids": [],
                        },
                        {
                            "statement": "strong one the model put second",
                            "supporting_event_ids": [
                                "aaaaaaaaaaaaaaaa",
                                "bbbbbbbbbbbbbbbb",
                            ],
                            "contradicting_event_ids": [],
                        },
                    ]
                }
            )
        ]
    )
    result = analyze(
        incident_id="INC-0001",
        events=events,
        chains=chains,
        provider=provider,
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    assert result.hypotheses[0].statement.startswith("strong")
    assert result.hypotheses[0].rank == 1
    assert result.hypotheses[0].confidence > result.hypotheses[1].confidence


def test_an_event_cannot_both_support_and_contradict(
    events: list[Event], chains: list[Chain], confidence_config: ConfidenceConfig
):
    provider = MockProvider(
        scripted=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "statement": "confused",
                            "supporting_event_ids": [
                                "aaaaaaaaaaaaaaaa",
                                "bbbbbbbbbbbbbbbb",
                            ],
                            "contradicting_event_ids": ["bbbbbbbbbbbbbbbb"],
                        }
                    ]
                }
            )
        ]
    )
    result = analyze(
        incident_id="INC-0001",
        events=events,
        chains=chains,
        provider=provider,
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    hypothesis = result.hypotheses[0]
    assert "bbbbbbbbbbbbbbbb" not in hypothesis.supporting_event_ids
    assert "bbbbbbbbbbbbbbbb" in hypothesis.contradicting_event_ids


def test_mock_provider_produces_at_least_two_hypotheses(
    events: list[Event], chains: list[Chain], confidence_config: ConfidenceConfig
):
    """Naming a runner-up is what exposes cases where the evidence was thin."""
    result = analyze(
        incident_id="INC-0001",
        events=events,
        chains=chains,
        provider=MockProvider(),
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    assert len(result.hypotheses) >= 2
    assert all(h.contradicting_event_ids is not None for h in result.hypotheses)


def test_no_events_means_no_hypotheses(confidence_config: ConfidenceConfig):
    result = analyze(
        incident_id="INC-0001",
        events=[],
        chains=[],
        provider=MockProvider(),
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    assert result.hypotheses == ()


def test_analyst_reports_token_usage(
    events: list[Event], chains: list[Chain], confidence_config: ConfidenceConfig
):
    result = analyze(
        incident_id="INC-0001",
        events=events,
        chains=chains,
        provider=MockProvider(),
        model="mock",
        confidence_config=confidence_config,
        sources_used=SOURCES,
    )
    assert result.input_tokens > 0


# ---------------------------------------------------------------------------
# code-rendered timeline
# ---------------------------------------------------------------------------
def test_timeline_lists_every_event_with_its_id(events: list[Event]):
    rendered = render_timeline(events)
    for item in events:
        assert item.id in rendered
    assert "## Timeline" in rendered


def test_timeline_is_sorted_regardless_of_input_order(events: list[Event]):
    rendered = render_timeline(list(reversed(events)))
    positions = [rendered.index(item.id) for item in events]
    assert positions == sorted(positions)


def test_timeline_shows_collapsed_occurrence_counts():
    collapsed = event("dddddddddddddddd", EventType.LOG_ERROR, 10)
    collapsed = Event(
        id=collapsed.id,
        type=collapsed.type,
        timestamp=collapsed.timestamp,
        source=collapsed.source,
        summary="pool exhausted",
        service="checkout",
        signature="pool exhausted",
        count=643,
    )
    assert "643" in render_timeline([collapsed])


def test_timeline_escapes_pipes_so_the_table_survives():
    noisy = Event(
        id="eeeeeeeeeeeeeeee",
        type=EventType.LOG_ERROR,
        timestamp=START,
        source="logs",
        summary="a | b | c",
    )
    assert r"a \| b \| c" in render_timeline([noisy])


def test_empty_timeline_says_so():
    assert "No events" in render_timeline([])


def test_evidence_for_model_puts_the_id_first(events: list[Event]):
    """So the model has no excuse for citing something it was not given."""
    first_line = render_evidence_for_model(events).splitlines()[0]
    assert first_line.startswith("- aaaaaaaaaaaaaaaa |")


def test_chains_for_model_show_which_heuristics_fired(chains: list[Chain]):
    rendered = render_chains_for_model(chains)
    assert "temporal_proximity, change_path_overlap" in rendered


def test_long_summaries_are_truncated():
    """Unbounded serialization makes token cost scale with log volume."""
    long_event = Event(
        id="ffffffffffffffff",
        type=EventType.LOG_ERROR,
        timestamp=START,
        source="logs",
        summary="x" * 5000,
    )
    assert len(render_evidence_for_model([long_event])) < 400


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------
def a_hypothesis(confidence: float, band: str) -> Hypothesis:
    return Hypothesis(
        id="hyp:1",
        rank=1,
        statement="the deploy removed DB_POOL_MAX",
        supporting_event_ids=("aaaaaaaaaaaaaaaa",),
        contradicting_event_ids=(),
        confidence=confidence,
        band=band,  # type: ignore[arg-type]
    )


def test_writer_is_told_the_band_so_it_can_hedge(events: list[Event]):
    provider = MockProvider()
    write_draft(
        incident_title="checkout 500s",
        events=events,
        hypotheses=[a_hypothesis(0.29, "tentative")],
        provider=provider,
        model="mock",
    )
    _, user = provider.calls[0]
    assert "tentative" in user
    assert "0.29" in user


def test_writer_only_receives_ids_it_may_cite(events: list[Event]):
    provider = MockProvider()
    write_draft(
        incident_title="checkout 500s",
        events=events,
        hypotheses=[a_hypothesis(0.9, "likely")],
        provider=provider,
        model="mock",
    )
    _, user = provider.calls[0]
    assert "Only these IDs exist" in user


def test_repair_pass_uses_the_repair_prompt_and_names_defects(events: list[Event]):
    provider = MockProvider()
    report = ValidationReport(
        incident_id="INC-0001",
        factual_sentences=10,
        cited_sentences=7,
        citations_total=8,
        citations_hallucinated=1,
        coverage_threshold=0.95,
        complaints=(
            Complaint(
                kind=ComplaintKind.HALLUCINATED_ID,
                detail="cited 9f2b000000000000 which does not exist",
                sentence_index=7,
                cited_id="9f2b000000000000",
            ),
        ),
    )
    write_draft(
        incident_title="checkout 500s",
        events=events,
        hypotheses=[a_hypothesis(0.9, "likely")],
        previous_draft="## Summary\n\nSomething happened.",
        validation=report,
        provider=provider,
        model="mock",
    )
    system, user = provider.calls[0]
    assert "failed mechanical validation" in system
    assert "hallucinated_id" in user
    assert "sentence 7" in user
    assert "Something happened" in user


def test_writer_told_plainly_when_there_are_no_hypotheses(events: list[Event]):
    provider = MockProvider()
    write_draft(
        incident_title="checkout 500s",
        events=events,
        hypotheses=[],
        provider=provider,
        model="mock",
    )
    _, user = provider.calls[0]
    assert "did not support any explanation" in user


# ---------------------------------------------------------------------------
# document assembly
# ---------------------------------------------------------------------------
@pytest.fixture
def incident() -> Incident:
    return Incident(
        id="INC-0001",
        title="checkout 500s after deploy",
        start_time=START,
        end_time=START + timedelta(hours=1),
        severity="sev2",
    )


def test_document_has_a_code_rendered_timeline(incident: Incident, events: list[Event]):
    document = render_document(
        incident=incident,
        draft_md="## Summary\n\nIt broke.\n\n## Hypotheses\n\nProbably the deploy.",
        events=events,
        hypotheses=[a_hypothesis(0.9, "likely")],
        sources_used=SOURCES,
    )
    assert "## Timeline" in document
    assert document.index("## Timeline") < document.index("## Hypotheses")


def test_a_model_written_timeline_is_replaced(incident: Incident, events: list[Event]):
    """A model-written timeline is a table of unchecked timestamps."""
    document = render_document(
        incident=incident,
        draft_md=(
            "## Summary\n\nIt broke.\n\n"
            "## Timeline\n\n- 09:99:99 something invented\n\n"
            "## Hypotheses\n\nProbably the deploy."
        ),
        events=events,
        hypotheses=[a_hypothesis(0.9, "likely")],
        sources_used=SOURCES,
    )
    assert "09:99:99" not in document
    assert document.count("## Timeline") == 1


def test_tentative_top_hypothesis_gets_an_inconclusive_banner(
    incident: Incident, events: list[Event]
):
    """docs/03: a system that admits inconclusiveness is more useful than one
    that always picks a winner."""
    document = render_document(
        incident=incident,
        draft_md="## Summary\n\nUnclear.",
        events=events,
        hypotheses=[a_hypothesis(0.29, "tentative")],
        sources_used=SOURCES,
    )
    assert "Analysis inconclusive" in document
    assert "needs human verification" in document


def test_confident_top_hypothesis_gets_no_banner(
    incident: Incident, events: list[Event]
):
    document = render_document(
        incident=incident,
        draft_md="## Summary\n\nThe deploy did it.",
        events=events,
        hypotheses=[a_hypothesis(0.94, "likely")],
        sources_used=SOURCES,
    )
    assert "Analysis inconclusive" not in document
    assert "Leading hypothesis" in document


def test_no_hypotheses_says_no_supported_explanation(
    incident: Incident, events: list[Event]
):
    document = render_document(
        incident=incident,
        draft_md="## Summary\n\nEvents were collected.",
        events=events,
        hypotheses=[],
        sources_used=SOURCES,
    )
    assert "No supported explanation" in document


def test_header_states_which_sources_were_available(
    incident: Incident, events: list[Event]
):
    """So a 0.8 from three sources is not mistaken for a 0.8 from five."""
    document = render_document(
        incident=incident,
        draft_md="## Summary\n\nIt broke.",
        events=events,
        hypotheses=[a_hypothesis(0.9, "likely")],
        sources_used=["logs", "git"],
        sources_failed=["alerts"],
    )
    assert "logs, git" in document
    assert "**Sources unavailable:** alerts" in document
    assert "excluded rather than counted against" in document
