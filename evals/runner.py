"""The eval harness (docs/07-evaluation.md, Phase 3.2).

Runs the real pipeline once per golden incident — the same
`build_deps`/`build_pipeline`/`initial_state` call shape `agent.cli.cmd_run`
and the integration tests already use, not a parallel implementation — scores
each run against its `expected.yaml` via `evals.metrics`, and writes a
timestamped report.

    python -m evals.runner --all
    python -m evals.runner --case inc-0004-two-deploys -v
    python -m evals.runner --all --mock                 # free, structural only
    python -m evals.runner --all --model claude-opus-5
    python -m evals.runner --all --sweep-weights         # expensive: reruns
                                                          # the suite per signal
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.cli import build_deps, load_incident_spec
from agent.config import SIGNAL_NAMES, Config, load_config
from agent.graph import build_pipeline, initial_state
from agent.memory import Neo4jMemory
from agent.observability.report import build_run_report, report_to_dict
from evals.metrics import (
    CaseResult,
    ExpectedLabel,
    SuiteResult,
    aggregate,
    score_case,
)

INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

EXIT_OK = 0
EXIT_CASES_FAILED = 1
EXIT_USAGE = 2


def discover_cases() -> list[Path]:
    """Every incident directory with an `expected.yaml` — the label is what
    makes a fixture a *golden* incident, not merely an incident directory."""
    if not INCIDENTS_DIR.is_dir():
        return []
    return sorted(
        d
        for d in INCIDENTS_DIR.iterdir()
        if d.is_dir() and (d / "expected.yaml").is_file()
    )


# ---------------------------------------------------------------------------
# running one case
# ---------------------------------------------------------------------------
def run_one_case(directory: Path, config: Config) -> tuple[CaseResult, dict[str, Any]]:
    """Run the real pipeline on one golden incident and score it.

    Returns the scored result plus a raw dict (the run report + resolved
    labels) for the per-case JSON output.
    """
    spec = load_incident_spec(directory)
    expected = ExpectedLabel.from_yaml(directory / "expected.yaml")

    started_at = datetime.now(UTC)
    with Neo4jMemory.from_config(config.neo4j) as memory:
        memory.ensure_schema()
        deps = build_deps(config, spec, memory)
        pipeline = build_pipeline(deps)
        final = pipeline.invoke(
            initial_state(spec.incident),
            {"recursion_limit": 12 + config.pipeline.max_validation_retries * 4},
        )
    finished_at = datetime.now(UTC)
    report = build_run_report(final, started_at=started_at, finished_at=finished_at)

    result = score_case(
        case_id=directory.name,
        expected=expected,
        hypotheses=list(final.get("hypotheses") or []),
        validation=final.get("validation"),
        document_md=final.get("document_md", ""),
        events=list(final.get("events") or []),
        token_usage=dict(report.token_usage),
        duration_seconds=report.duration_seconds,
        model=config.llm.writer_model,
    )
    raw = {
        "case_id": directory.name,
        "report": report_to_dict(report),
        "metrics": dataclasses.asdict(result),
    }
    return result, raw


def run_suite(cases: list[Path], config: Config, *, verbose: bool) -> list[CaseResult]:
    results: list[CaseResult] = []
    for directory in cases:
        result, _ = run_one_case(directory, config)
        results.append(result)
        _print_case_line(result, verbose=verbose)
    return results


# ---------------------------------------------------------------------------
# --sweep-weights
# ---------------------------------------------------------------------------
def sweep_weights(
    cases: list[Path], config: Config
) -> dict[str, dict[str, SuiteResult]]:
    """For each confidence signal, rerun the suite with that weight scaled up
    (x1.5) and down (x0.5), all other weights unchanged — the model has no
    sum-to-1 constraint (`ConfidenceConfig.__post_init__` only requires
    exactly `SIGNAL_NAMES`' keys), so no renormalization is needed. Expensive
    — this multiplies the run count by `2 x len(SIGNAL_NAMES)` — which is
    exactly why it's an opt-in flag, not the default path."""
    baseline_weights = config.confidence.weights
    swept: dict[str, dict[str, SuiteResult]] = {}
    for signal in sorted(SIGNAL_NAMES):
        variants: dict[str, SuiteResult] = {}
        for label, factor in (("high", 1.5), ("low", 0.5)):
            weights = dict(baseline_weights)
            weights[signal] = round(weights[signal] * factor, 4)
            perturbed_confidence = dataclasses.replace(
                config.confidence, weights=weights
            )
            perturbed_config = dataclasses.replace(
                config, confidence=perturbed_confidence
            )
            print(f"\n-- sweeping {signal} ({label}, x{factor}) --")
            results = run_suite(cases, perturbed_config, verbose=False)
            variants[label] = aggregate(results)
        swept[signal] = variants
    return swept


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _print_case_line(result: CaseResult, *, verbose: bool) -> None:
    p1 = "✓" if result.precision_at_1 else "—"
    decoy = "✓" if result.decoy_resistance else "—"
    print(
        f"{result.case_id:<30} {p1:^5} {result.citation_coverage:>5.3f}  "
        f"{result.hallucinated_citation_rate:>5.3f}   {decoy:^5}"
        f"{result.confidence:>6.2f}  {result.band}"
    )
    if result.unresolved_labels:
        for label in result.unresolved_labels:
            print(f"  ! unresolved label: {label}", file=sys.stderr)
    if verbose and result.must_cite_recall < 1.0:
        print(f"  must-cite recall: {result.must_cite_recall:.2f}")


def render_report(suite: SuiteResult, *, model: str, git_sha: str) -> str:
    lines = [
        f"# Eval report — {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "",
        f"Model: `{model}` · Commit: `{git_sha}` · Cases: {len(suite.case_results)}",
        "",
        "```",
        f"{'CASE':<30} {'P@1':^5} {'COV':>6} {'HALL':>6}  {'DECOY':^5}"
        f"{'CONF':>6}  BAND",
    ]
    for case in suite.case_results:
        p1 = "✓" if case.precision_at_1 else "—"
        decoy = "✓" if case.decoy_resistance else "—"
        lines.append(
            f"{case.case_id:<30} {p1:^5} {case.citation_coverage:>6.3f} "
            f"{case.hallucinated_citation_rate:>6.3f}   {decoy:^5}"
            f"{case.confidence:>6.2f}  {case.band}"
        )
    lines.append("─" * 72)
    abst = (
        f"{suite.abstention_correctness:.2f}"
        if suite.abstention_correctness is not None
        else "n/a"
    )
    conf_sep = (
        f"{suite.confidence_separation:.2f}"
        if suite.confidence_separation is not None
        else "n/a"
    )
    cost = f"${suite.cost_usd:.2f}" if suite.cost_usd is not None else "n/a"
    lines += [
        f"P@1 {suite.precision_at_1:.2f} │ R@3 {suite.recall_at_3:.2f} │ "
        f"COV {suite.citation_coverage:.3f} │ "
        f"HALL {suite.hallucinated_citation_rate:.3f} │ "
        f"DECOY {suite.decoy_resistance:.2f} │ ABST {abst}",
        f"Tokens {suite.tokens_total:,} │ Cost {cost} │ "
        f"Conf.sep {conf_sep} │ "
        f"p50 {suite.latency_p50:.0f}s │ p95 {suite.latency_p95:.0f}s",
        "```",
    ]
    unresolved = [
        (case.case_id, label)
        for case in suite.case_results
        for label in case.unresolved_labels
    ]
    if unresolved:
        lines += ["", "## Unresolved labels (fixture bugs, not model failures)", ""]
        lines += [f"- `{case_id}`: {label}" for case_id, label in unresolved]
    return "\n".join(lines) + "\n"


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return "unknown"


def write_results(
    suite: SuiteResult,
    raw_cases: list[dict[str, Any]],
    *,
    model: str,
    output_root: Path = RESULTS_DIR,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = output_root / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    for raw in raw_cases:
        case_path = out_dir / f"{raw['case_id']}.json"
        case_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

    git_sha = _git_sha()
    report_md = render_report(suite, model=model, git_sha=git_sha)
    (out_dir / "report.md").write_text(report_md, encoding="utf-8")
    print(f"\n{report_md}")

    history_line = {
        "timestamp": timestamp,
        "git_sha": git_sha,
        "model": model,
        "cases": len(suite.case_results),
        "precision_at_1": suite.precision_at_1,
        "recall_at_3": suite.recall_at_3,
        "citation_coverage": suite.citation_coverage,
        "hallucinated_citation_rate": suite.hallucinated_citation_rate,
        "decoy_resistance": suite.decoy_resistance,
        "abstention_correctness": suite.abstention_correctness,
        "confidence_separation": suite.confidence_separation,
        "tokens_total": suite.tokens_total,
        "cost_usd": suite.cost_usd,
        "latency_p50": suite.latency_p50,
        "latency_p95": suite.latency_p95,
    }
    history_path = output_root / "history.jsonl"
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(history_line) + "\n")

    return out_dir


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evals.runner", description="Score the pipeline against golden incidents."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="run every golden incident")
    group.add_argument("--case", type=str, help="run one incident by directory name")
    parser.add_argument(
        "--model", type=str, default=None, help="override ANALYST_MODEL/WRITER_MODEL"
    )
    parser.add_argument(
        "--mock", action="store_true", help="force LLM_PROVIDER=mock (free, no tokens)"
    )
    parser.add_argument(
        "--sweep-weights",
        action="store_true",
        help="rerun the suite per confidence signal, scaled up and down (expensive)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.mock:
        os.environ["LLM_PROVIDER"] = "mock"
    config = load_config()
    if args.mock:
        # `.env`'s own ANALYST_MODEL/WRITER_MODEL (e.g. a real OpenRouter
        # model) would otherwise win over the provider default — correct
        # for a real run, misleading here: the mock provider ignores the
        # model string entirely, so the report should say "mock", not
        # whatever model happens to be configured for real runs.
        config = dataclasses.replace(
            config,
            llm=dataclasses.replace(
                config.llm, analyst_model="mock", writer_model="mock"
            ),
        )
    if args.model:
        config = dataclasses.replace(
            config,
            llm=dataclasses.replace(
                config.llm, analyst_model=args.model, writer_model=args.model
            ),
        )

    all_cases = discover_cases()
    if args.case:
        cases = [d for d in all_cases if d.name == args.case]
        if not cases:
            print(
                f"no golden incident named {args.case!r} in {INCIDENTS_DIR}",
                file=sys.stderr,
            )
            return EXIT_USAGE
    else:
        cases = all_cases
        if not cases:
            print(
                f"no golden incidents with expected.yaml found in {INCIDENTS_DIR}",
                file=sys.stderr,
            )
            return EXIT_USAGE

    if args.sweep_weights:
        sweep_weights(cases, config)
        return EXIT_OK

    results: list[CaseResult] = []
    raw_cases: list[dict[str, Any]] = []
    for directory in cases:
        result, raw = run_one_case(directory, config)
        results.append(result)
        raw_cases.append(raw)
        _print_case_line(result, verbose=args.verbose)

    suite = aggregate(results)
    write_results(suite, raw_cases, model=config.llm.writer_model)

    any_unresolved = any(r.unresolved_labels for r in results)
    any_hallucinated = any(r.hallucinated_citation_rate > 0 for r in results)
    if any_unresolved or any_hallucinated:
        return EXIT_CASES_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
