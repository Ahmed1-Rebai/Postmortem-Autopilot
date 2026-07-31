"""The entry point: `python -m agent.cli <command>`.

Commands
--------
`schema-init`  create Neo4j constraints and indexes (idempotent)
`run`          collect → graph → link → analyze → write → validate → publish
`validate`     re-check an existing document against the graph
`sweep`        finish postmortems for incidents a run never completed

The eval harness lives outside this entry point — `python -m evals.runner`,
not `agent.cli eval` (docs/07-evaluation.md).

Exit codes matter here. A run whose document failed validation exits non-zero
*and still writes the draft and the report to disk* — invariant 8: fail loudly,
never publish a failing draft, and never quietly lower the bar. The artifacts
are written precisely because someone has to be able to see what was rejected.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from agent.collectors.alerts import AlertCollector
from agent.collectors.base import Collector
from agent.collectors.git import GitCollector
from agent.collectors.logs import LogCollector
from agent.config import Config, ConfigError, load_config
from agent.graph import (
    PipelineDeps,
    build_pipeline,
    build_recovery_pipeline,
    initial_state,
)
from agent.linker import CandidateLinker, LinkerConfig
from agent.llm import LLMError, build_provider
from agent.memory import GraphStateError, Neo4jMemory
from agent.nodes.recurrence import persist_corrective_actions
from agent.nodes.validator import validate as run_validator
from agent.normalize.services import ServiceCanonicalizer
from agent.observability.metrics import observe_run, push_metrics, write_metrics
from agent.observability.report import (
    build_run_report,
    write_document,
    write_run_report,
)
from agent.render.document import render_run_report_summary
from agent.state import Incident

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE = 2
EXIT_RUNTIME = 3


@dataclass(frozen=True, slots=True)
class IncidentSpec:
    """One golden-incident directory, as described by its `meta.yaml`."""

    incident: Incident
    directory: Path
    logs_path: Path
    repo_path: Path
    alerts_path: Path


def load_incident_spec(directory: Path) -> IncidentSpec:
    """Read `meta.yaml` and resolve the fixture paths beside it."""
    meta_path = directory / "meta.yaml"
    if not meta_path.is_file():
        raise ConfigError(f"no meta.yaml in {directory}")
    try:
        raw = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{meta_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{meta_path} must be a mapping")

    missing = {"id", "title", "start", "end"} - raw.keys()
    if missing:
        raise ConfigError(f"{meta_path} is missing: {sorted(missing)}")

    incident = Incident(
        id=str(raw["id"]),
        title=str(raw["title"]),
        start_time=_parse_time(raw["start"], f"{meta_path}:start"),
        end_time=_parse_time(raw["end"], f"{meta_path}:end"),
        severity=str(raw.get("severity", "unknown")),
        status=str(raw.get("status", "closed")),
    )
    return IncidentSpec(
        incident=incident,
        directory=directory,
        logs_path=directory / str(raw.get("logs", "logs")),
        repo_path=directory / str(raw.get("repo", "repo")),
        alerts_path=directory / str(raw.get("alerts", "alerts.json")),
    )


def _parse_time(value: Any, where: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"{where}: {value!r} is not an ISO timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_collectors(spec: IncidentSpec) -> list[Collector]:
    return [
        LogCollector(spec.logs_path),
        GitCollector(spec.repo_path),
        AlertCollector(spec.alerts_path),
    ]


def build_deps(config: Config, spec: IncidentSpec, memory: Neo4jMemory) -> PipelineDeps:
    canonicalizer = ServiceCanonicalizer.from_config(
        config.services.aliases, config.services.strip_suffixes
    )
    return PipelineDeps(
        config=config,
        memory=memory,
        provider=build_provider(config.llm),
        collectors=build_collectors(spec),
        canonicalizer=canonicalizer,
        linker=CandidateLinker(
            LinkerConfig.from_pipeline_config(config.pipeline), canonicalizer
        ),
    )


def build_recovery_deps(config: Config, memory: Neo4jMemory) -> PipelineDeps:
    """Like `build_deps`, without an `IncidentSpec` — the sweep's recovery
    pipeline never collects, so there's no directory to build collectors
    from. `collectors=()` is safe: `build_recovery_pipeline` never adds a
    `collect` node, so nothing ever iterates them."""
    canonicalizer = ServiceCanonicalizer.from_config(
        config.services.aliases, config.services.strip_suffixes
    )
    return PipelineDeps(
        config=config,
        memory=memory,
        provider=build_provider(config.llm),
        collectors=(),
        canonicalizer=canonicalizer,
        linker=CandidateLinker(
            LinkerConfig.from_pipeline_config(config.pipeline), canonicalizer
        ),
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_schema_init(config: Config) -> int:
    with Neo4jMemory.from_config(config.neo4j) as memory:
        memory.verify_connectivity()
        memory.ensure_schema()
    print(f"schema ready at {config.neo4j.uri}")
    return EXIT_OK


def cmd_run(config: Config, incident_dir: Path, metrics_path: str | None) -> int:
    spec = load_incident_spec(incident_dir)
    started_at = datetime.now(UTC)

    with Neo4jMemory.from_config(config.neo4j) as memory:
        memory.ensure_schema()
        deps = build_deps(config, spec, memory)
        pipeline = build_pipeline(deps)
        # recursion_limit bounds the graph, not just the retry counter: a
        # miscounted retry must not become an unbounded model spend.
        final = pipeline.invoke(
            initial_state(spec.incident),
            {"recursion_limit": 12 + config.pipeline.max_validation_retries * 4},
        )

        finished_at = datetime.now(UTC)
        report = build_run_report(final, started_at=started_at, finished_at=finished_at)

        # Written pass or fail. A rejected draft is exactly what someone needs
        # to look at, so withholding it would be the unhelpful choice.
        document_path = write_document(
            spec.incident.id, final.get("document_md", ""), config.output_dir
        )
        report_path = write_run_report(report, config.output_dir)

        if report.succeeded:
            memory.persist_hypotheses(
                spec.incident.id,
                list(final.get("hypotheses") or []),
                now=finished_at,
            )
            memory.link_similar_incidents(spec.incident.id)
            # Only a published document's Corrective Actions bullets become
            # graph facts — a rejected draft's proposals were never checked.
            persist_corrective_actions(
                memory, spec.incident.id, final.get("document_md", "")
            )

    validation = report.validation
    observe_run(
        succeeded=report.succeeded,
        duration_seconds=report.duration_seconds,
        event_count=report.event_count,
        candidate_count=report.candidate_count,
        retry_count=report.retry_count,
        coverage=validation.coverage if validation else None,
        hallucinated=validation.citations_hallucinated if validation else 0,
        token_usage=dict(report.token_usage),
        failed_sources=[failure.source for failure in report.sources_failed],
    )
    if metrics_path:
        write_metrics(metrics_path)
    if config.pushgateway_url:
        push_metrics(config.pushgateway_url, report.incident_id)

    print(render_run_report_summary(report))
    print(f"  document: {document_path}")
    print(f"  report:   {report_path}")

    if not report.succeeded:
        print(
            "\nVALIDATION FAILED — the draft was not published.",
            file=sys.stderr,
        )
        for complaint in (validation.complaints if validation else ())[:10]:
            where = (
                f"sentence {complaint.sentence_index}"
                if complaint.sentence_index is not None
                else "document"
            )
            print(
                f"  [{complaint.kind.value}] {where}: {complaint.detail}",
                file=sys.stderr,
            )
        return EXIT_VALIDATION_FAILED
    return EXIT_OK


def cmd_sweep(config: Config, hours: int, metrics_path: str | None) -> int:
    """The nightly CronJob's job: find incidents that started a run (wrote
    events) but never finished one, and finish it — resuming from the graph,
    not re-collecting. See `Neo4jMemory.find_incidents_needing_postmortem`
    for exactly what this does and doesn't catch."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    run_started_at = datetime.now(UTC)
    any_failed = False

    with Neo4jMemory.from_config(config.neo4j) as memory:
        memory.ensure_schema()
        candidates = memory.find_incidents_needing_postmortem(since=since)
        if not candidates:
            print(f"sweep: no incidents needing a postmortem in the last {hours}h")
            return EXIT_OK
        print(f"sweep: {len(candidates)} incident(s) needing a postmortem")

        deps = build_recovery_deps(config, memory)
        pipeline = build_recovery_pipeline(deps)

        for incident in candidates:
            events = memory.get_timeline(incident.id)
            if not events:
                # An Incident node only exists once build_graph wrote >=1
                # event, so this shouldn't happen — but skip rather than
                # crash the whole batch on graph state this code doesn't
                # expect.
                print(
                    f"  {incident.id}: skipped, no events in the graph",
                    file=sys.stderr,
                )
                any_failed = True
                continue
            sources_used = sorted({e.source for e in events})

            started_at = datetime.now(UTC)
            final = pipeline.invoke(
                initial_state(incident, events=events, sources_used=sources_used),
                {"recursion_limit": 12 + config.pipeline.max_validation_retries * 4},
            )
            finished_at = datetime.now(UTC)
            report = build_run_report(
                final, started_at=started_at, finished_at=finished_at
            )

            document_path = write_document(
                incident.id, final.get("document_md", ""), config.output_dir
            )
            report_path = write_run_report(report, config.output_dir)

            if report.succeeded:
                memory.persist_hypotheses(
                    incident.id, list(final.get("hypotheses") or []), now=finished_at
                )
                memory.link_similar_incidents(incident.id)
                persist_corrective_actions(
                    memory, incident.id, final.get("document_md", "")
                )
            else:
                any_failed = True

            validation = report.validation
            observe_run(
                succeeded=report.succeeded,
                duration_seconds=report.duration_seconds,
                event_count=report.event_count,
                candidate_count=report.candidate_count,
                retry_count=report.retry_count,
                coverage=validation.coverage if validation else None,
                hallucinated=validation.citations_hallucinated if validation else 0,
                token_usage=dict(report.token_usage),
                failed_sources=[failure.source for failure in report.sources_failed],
            )
            print(f"  {render_run_report_summary(report)}")
            print(f"    document: {document_path}")
            print(f"    report:   {report_path}")

    if metrics_path:
        write_metrics(metrics_path)
    if config.pushgateway_url:
        # One push for the whole batch, not per incident: observe_run above
        # was called once per incident and the counters are cumulative for
        # the life of this process, so a per-incident push here would carry
        # every earlier incident's counts too. Grouped under a batch id
        # instead of an incident id — a sweep run is naturally a batch
        # metric, not a single-incident one.
        push_metrics(
            config.pushgateway_url, f"sweep-{run_started_at.strftime('%Y%m%dT%H%M%SZ')}"
        )

    return EXIT_VALIDATION_FAILED if any_failed else EXIT_OK


def cmd_validate(config: Config, incident_id: str, document_path: Path) -> int:
    if not document_path.is_file():
        print(f"no such document: {document_path}", file=sys.stderr)
        return EXIT_USAGE

    document = document_path.read_text(encoding="utf-8")
    with Neo4jMemory.from_config(config.neo4j) as memory:
        report = run_validator(
            incident_id=incident_id,
            document=document,
            resolve=lambda ids: memory.resolve_citations(incident_id, list(ids)),
            coverage_threshold=config.pipeline.citation_coverage_threshold,
        )

    print(
        f"{incident_id}: {'PASS' if report.passed else 'FAIL'} · "
        f"coverage {report.coverage:.1%} "
        f"({report.cited_sentences}/{report.factual_sentences}) · "
        f"hallucinated {report.citations_hallucinated} · "
        f"timestamp mismatches {report.timestamp_mismatches}"
    )
    for complaint in report.complaints:
        where = (
            f"sentence {complaint.sentence_index}"
            if complaint.sentence_index is not None
            else "document"
        )
        print(f"  [{complaint.kind.value}] {where}: {complaint.detail}")
    return EXIT_OK if report.passed else EXIT_VALIDATION_FAILED


def cmd_eval() -> int:
    print(
        "the eval harness is its own entry point, not a subcommand of this "
        "one: `python -m evals.runner --all` (see docs/07-evaluation.md).",
        file=sys.stderr,
    )
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.cli", description="Evidence-grounded incident postmortems."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("schema-init", help="create Neo4j constraints and indexes")

    run = sub.add_parser("run", help="generate a postmortem for one incident")
    run.add_argument("--incident", required=True, type=Path, help="incident directory")
    run.add_argument(
        "--metrics",
        type=str,
        default=None,
        help="write Prometheus textfile metrics to this path",
    )

    validate = sub.add_parser("validate", help="re-check an existing document")
    validate.add_argument("--incident-id", required=True)
    validate.add_argument("--document", required=True, type=Path)

    sweep = sub.add_parser(
        "sweep", help="finish postmortems for incidents a run never completed"
    )
    sweep.add_argument(
        "--hours",
        type=int,
        default=24,
        help="how far back to look for closed incidents (default 24)",
    )
    sweep.add_argument(
        "--metrics",
        type=str,
        default=None,
        help="write Prometheus textfile metrics to this path",
    )

    sub.add_parser("eval", help="use `python -m evals.runner` instead — see docs/07")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "eval":
        return cmd_eval()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.command == "schema-init":
            return cmd_schema_init(config)
        if args.command == "run":
            return cmd_run(config, args.incident, args.metrics)
        if args.command == "validate":
            return cmd_validate(config, args.incident_id, args.document)
        if args.command == "sweep":
            return cmd_sweep(config, args.hours, args.metrics)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (GraphStateError, LLMError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except RuntimeError as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
