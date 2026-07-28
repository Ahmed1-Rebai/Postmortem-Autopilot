"""Prometheus metrics. Every name is prefixed `pm_`.

The one that matters is `pm_hallucinated_citations_total`. It is not a quality
metric with a target to improve — it is the invariant the architecture exists
to guarantee, and Phase 2 alerts on any increase at all.

Metrics are registered at import and are no-ops until something observes them,
so importing this module in a test costs nothing.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    write_to_textfile,
)

REGISTRY: Final[CollectorRegistry] = CollectorRegistry()

RUNS: Final[Counter] = Counter(
    "pm_runs_total",
    "Pipeline runs, by outcome.",
    labelnames=("status",),
    registry=REGISTRY,
)

RUN_DURATION: Final[Histogram] = Histogram(
    "pm_run_duration_seconds",
    "Wall-clock duration of a full pipeline run.",
    buckets=(5, 10, 20, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)

EVENTS_COLLECTED: Final[Histogram] = Histogram(
    "pm_events_collected",
    "Events written to the graph per run, after signature collapsing.",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
    registry=REGISTRY,
)

CANDIDATE_LINKS: Final[Histogram] = Histogram(
    "pm_candidate_links",
    "Candidate causal links proposed per run.",
    buckets=(1, 5, 10, 20, 50, 100, 200),
    registry=REGISTRY,
)

CITATION_COVERAGE: Final[Histogram] = Histogram(
    "pm_citation_coverage",
    "Fraction of factual sentences carrying a valid citation.",
    buckets=(0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 1.0),
    registry=REGISTRY,
)

#: Must stay at zero. Any increase means the verification plane has a bug.
HALLUCINATED_CITATIONS: Final[Counter] = Counter(
    "pm_hallucinated_citations_total",
    "Cited IDs that did not resolve to an event in the incident.",
    registry=REGISTRY,
)

VALIDATION_RETRIES: Final[Histogram] = Histogram(
    "pm_validation_retries",
    "Writer rewrites triggered by the validator, per run.",
    buckets=(0, 1, 2),
    registry=REGISTRY,
)

LLM_TOKENS: Final[Counter] = Counter(
    "pm_llm_tokens_total",
    "Model tokens consumed, by pipeline node.",
    labelnames=("node",),
    registry=REGISTRY,
)

COLLECTOR_FAILURES: Final[Counter] = Counter(
    "pm_collector_failures_total",
    "Collectors that errored and were skipped.",
    labelnames=("source",),
    registry=REGISTRY,
)


def observe_run(
    *,
    succeeded: bool,
    duration_seconds: float,
    event_count: int,
    candidate_count: int,
    retry_count: int,
    coverage: float | None,
    hallucinated: int,
    token_usage: dict[str, int],
    failed_sources: list[str],
) -> None:
    """Record one finished run. Called by the CLI, pass or fail."""
    RUNS.labels(status="success" if succeeded else "failure").inc()
    RUN_DURATION.observe(duration_seconds)
    EVENTS_COLLECTED.observe(event_count)
    CANDIDATE_LINKS.observe(candidate_count)
    VALIDATION_RETRIES.observe(retry_count)
    if coverage is not None:
        CITATION_COVERAGE.observe(coverage)
    if hallucinated:
        HALLUCINATED_CITATIONS.inc(hallucinated)
    for node, tokens in token_usage.items():
        LLM_TOKENS.labels(node=node).inc(tokens)
    for source in failed_sources:
        COLLECTOR_FAILURES.labels(source=source).inc()


def write_metrics(path: str) -> None:
    """Write the textfile-collector format.

    A batch Job has nowhere to be scraped from, so it writes a file that a
    node_exporter textfile collector picks up. Phase 2 mounts the directory.
    """
    write_to_textfile(path, REGISTRY)
