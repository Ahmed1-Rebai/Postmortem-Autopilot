"""The pipeline, as a LangGraph DAG.

```
collect -> build_graph -> link_candidates -> recall_similar -> analyze
                                                                 |
                                      +--------------------> write
                                      |                        |
                                 (retry, max 2)                v
                                      |                    validate
                                      +------- fail ----------+
                                                              | pass
                                                              v
                                                           publish
```

A fixed DAG with one bounded retry edge, not an agent with tools (ADR-003).
An incident analyzer that can wander is one that cannot be evaluated, and the
eval harness is the point.

Only `analyze` and `write` call a model — two calls in the happy path. The
retry edge is the only cycle, and it is bounded by config, which is what keeps
a bad draft from becoming an expensive loop.

Dependencies are injected into the node closures rather than imported, so the
whole graph can be run against a mock provider and a throwaway Neo4j.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from agent.collectors.base import Collector, CollectorError, RawRecord
from agent.config import Config
from agent.linker import CandidateLinker
from agent.llm import LLMProvider
from agent.memory import Neo4jMemory
from agent.nodes.analyst import analyze as run_analyst
from agent.nodes.validator import validate as run_validator
from agent.nodes.writer import write_draft
from agent.normalize.events import normalize_records
from agent.normalize.services import ServiceCanonicalizer
from agent.render.document import render_document
from agent.state import (
    Incident,
    PipelineState,
    SourceFailure,
    Window,
)


@dataclass(frozen=True, slots=True)
class PipelineDeps:
    """Everything the nodes need, assembled once by the CLI."""

    config: Config
    memory: Neo4jMemory
    provider: LLMProvider
    collectors: Sequence[Collector]
    canonicalizer: ServiceCanonicalizer
    linker: CandidateLinker
    #: Overridable so tests can assert on timing without sleeping.
    now: Any = field(default=lambda: datetime.now(UTC))


def build_pipeline(deps: PipelineDeps) -> Any:
    """Compile the DAG. Returns a LangGraph runnable."""
    graph: StateGraph[PipelineState, Any, Any, Any] = StateGraph(PipelineState)

    graph.add_node("collect", _collect_node(deps))
    graph.add_node("build_graph", _build_graph_node(deps))
    graph.add_node("link_candidates", _link_candidates_node(deps))
    graph.add_node("recall_similar", _recall_similar_node(deps))
    graph.add_node("analyze", _analyze_node(deps))
    graph.add_node("write", _write_node(deps))
    graph.add_node("validate", _validate_node(deps))
    graph.add_node("publish", _publish_node())

    graph.add_edge(START, "collect")
    graph.add_edge("collect", "build_graph")
    graph.add_edge("build_graph", "link_candidates")
    graph.add_edge("link_candidates", "recall_similar")
    graph.add_edge("recall_similar", "analyze")
    graph.add_edge("analyze", "write")
    graph.add_edge("write", "validate")
    graph.add_conditional_edges(
        "validate",
        _after_validation(deps.config.pipeline.max_validation_retries),
        {"retry": "write", "publish": "publish", "fail": END},
    )
    graph.add_edge("publish", END)
    return graph.compile()


def _after_validation(
    max_retries: int,
) -> Any:
    """The only branch in the pipeline.

    Invariant 8: the threshold is never lowered and the retry count is never
    exceeded. Falling off the end without publishing is the correct outcome for
    a draft that could not be repaired — the CLI turns it into a non-zero exit.
    """

    def decide(state: PipelineState) -> Literal["retry", "publish", "fail"]:
        report = state.get("validation")
        if report is not None and report.passed:
            return "publish"
        if state.get("retry_count", 0) < max_retries:
            return "retry"
        return "fail"

    return decide


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------
def _collect_node(deps: PipelineDeps) -> Any:
    def collect(state: PipelineState) -> dict[str, Any]:
        window = _window_of(state)
        records: list[RawRecord] = []
        used: list[str] = []
        failed: list[SourceFailure] = []

        for collector in deps.collectors:
            try:
                got = collector.collect(window, None)
            except CollectorError as exc:
                # A failing collector degrades the run rather than aborting it.
                # The gap is recorded, and the confidence denominator shrinks
                # accordingly — a missing source lowers certainty, it does not
                # invent it.
                failed.append(SourceFailure(source=collector.name, error=str(exc)))
                continue
            records.extend(got)
            used.append(collector.name)

        events = normalize_records(records, deps.canonicalizer)
        limit = deps.config.pipeline.max_events_per_run
        if len(events) > limit:
            events = events[:limit]
        return {"events": events, "sources_used": used, "sources_failed": failed}

    return collect


def _build_graph_node(deps: PipelineDeps) -> Any:
    def build_graph(state: PipelineState) -> dict[str, Any]:
        events = state.get("events") or []
        if not events:
            # Zero events is a config error, not a quiet incident. Aborting is
            # correct: there is nothing to analyze and nothing to cite.
            raise RuntimeError(
                "no events were collected — check the incident's source paths "
                "and window before treating this as an empty incident"
            )
        incident = _incident_of(state)
        deps.memory.upsert_incident(incident)
        deps.memory.write_events(incident.id, list(events))
        deps.memory.link_temporal(incident.id)
        return {"graph_ready": True}

    return build_graph


def _link_candidates_node(deps: PipelineDeps) -> Any:
    def link_candidates(state: PipelineState) -> dict[str, Any]:
        events = list(state.get("events") or [])
        links = deps.linker.link(events)
        for link in links:
            deps.memory.link_candidate(
                link.cause_id,
                link.effect_id,
                list(link.heuristics),
                link.delta_seconds,
                link.score,
            )
        return {"candidates": links}

    return link_candidates


def _recall_similar_node(deps: PipelineDeps) -> Any:
    def recall_similar(state: PipelineState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        deps.memory.compute_fingerprint(incident_id)
        return {"similar_incidents": deps.memory.find_similar_incidents(incident_id)}

    return recall_similar


def _analyze_node(deps: PipelineDeps) -> Any:
    def analyze(state: PipelineState) -> dict[str, Any]:
        result = run_analyst(
            incident_id=state["incident_id"],
            events=list(state.get("events") or []),
            chains=deps.memory.get_candidate_chains(state["incident_id"]),
            provider=deps.provider,
            model=deps.config.llm.analyst_model,
            confidence_config=deps.config.confidence,
            sources_used=list(state.get("sources_used") or []),
        )
        return {
            "hypotheses": result.hypotheses,
            "token_usage": _add_tokens(
                state, analyst=result.input_tokens + result.output_tokens
            ),
        }

    return analyze


def _write_node(deps: PipelineDeps) -> Any:
    def write(state: PipelineState) -> dict[str, Any]:
        report = state.get("validation")
        previous = state.get("draft_md")
        repairing = report is not None and not report.passed and bool(previous)

        result = write_draft(
            incident_title=_incident_of(state).title,
            events=list(state.get("events") or []),
            hypotheses=list(state.get("hypotheses") or []),
            previous_draft=previous if repairing else None,
            validation=report if repairing else None,
            provider=deps.provider,
            model=deps.config.llm.writer_model,
        )
        return {
            "draft_md": result.draft_md,
            "retry_count": state.get("retry_count", 0) + (1 if repairing else 0),
            "token_usage": _add_tokens(
                state, writer=result.input_tokens + result.output_tokens
            ),
        }

    return write


def _validate_node(deps: PipelineDeps) -> Any:
    def validate(state: PipelineState) -> dict[str, Any]:
        incident = _incident_of(state)
        document = render_document(
            incident=incident,
            draft_md=state.get("draft_md", ""),
            events=list(state.get("events") or []),
            hypotheses=list(state.get("hypotheses") or []),
            sources_used=list(state.get("sources_used") or []),
            sources_failed=[f.source for f in state.get("sources_failed") or []],
            similar_incidents=list(state.get("similar_incidents") or []),
            generated_at=deps.now(),
        )
        # The assembled document is what gets checked, so nothing ships that
        # the verification plane has not seen.
        report = run_validator(
            incident_id=incident.id,
            document=document,
            resolve=lambda ids: deps.memory.resolve_citations(incident.id, list(ids)),
            coverage_threshold=deps.config.pipeline.citation_coverage_threshold,
        )
        return {"document_md": document, "validation": report}

    return validate


def _publish_node() -> Any:
    def publish(state: PipelineState) -> dict[str, Any]:
        # Persisting hypotheses only on success keeps the graph's record of
        # past reasoning free of drafts that were never allowed to ship.
        return {"document_md": state.get("document_md", "")}

    return publish


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _window_of(state: PipelineState) -> Window:
    window = state.get("window")
    if window is None:
        incident = _incident_of(state)
        return Window(incident.start_time, incident.end_time)
    return Window(window[0], window[1])


def _incident_of(state: PipelineState) -> Incident:
    incident = state.get("incident")
    if incident is None:
        raise RuntimeError("pipeline state has no incident")
    return incident


def _add_tokens(state: PipelineState, **added: int) -> dict[str, int]:
    usage = dict(state.get("token_usage") or {})
    for key, value in added.items():
        usage[key] = usage.get(key, 0) + value
    return usage


def initial_state(incident: Incident) -> PipelineState:
    return PipelineState(
        incident_id=incident.id,
        incident=incident,
        window=(incident.start_time, incident.end_time),
        events=[],
        graph_ready=False,
        candidates=[],
        hypotheses=[],
        similar_incidents=[],
        draft_md="",
        document_md="",
        validation=None,
        retry_count=0,
        token_usage={},
        sources_used=[],
        sources_failed=[],
    )
