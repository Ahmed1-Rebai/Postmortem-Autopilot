"""Regenerate the eval-metrics table in the README from history.jsonl.

    python -m evals.update_readme

Reads the latest committed run in `evals/results/history.jsonl` and replaces
the block between `<!-- EVAL-METRICS:START -->` / `<!-- EVAL-METRICS:END -->`
markers in README.md. That keeps the headline numbers a fact about the latest
run, not a screenshot that rots — and because history.jsonl is the one
committed artifact (`.gitignore`), the table and the trend data can't drift
apart.

Runs after the full eval in CI (nightly job) and locally after `--all --gate`.
Exits 1 if the markers are missing from README.md — a silent no-op would let a
renamed section quietly stop being updated.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import evals.metrics as m
from evals.runner import RESULTS_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
README = PROJECT_ROOT / "README.md"

MARKER_START = "<!-- EVAL-METRICS:START -->"
MARKER_END = "<!-- EVAL-METRICS:END -->"


def _ts(iso: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        )
    except ValueError:
        return iso


def render_table(entry: dict[str, Any]) -> str:
    """One markdown table row for a history.jsonl entry, gates annotated."""
    abst = (
        f"{entry['abstention_correctness']:.2f}"
        if entry.get("abstention_correctness") is not None
        else "n/a"
    )
    cost = f"${entry['cost_usd']:.2f}" if entry.get("cost_usd") is not None else "n/a"
    conf_sep = (
        f"{entry['confidence_separation']:.2f}"
        if entry.get("confidence_separation") is not None
        else "n/a"
    )
    return (
        f"| `{_ts(entry['timestamp'])}` | {entry['model']} | `{entry['git_sha']}` | "
        f"{entry['cases']} | {entry['precision_at_1']:.2f} | "
        f"{entry['recall_at_3']:.2f} | {entry['citation_coverage']:.3f} | "
        f"{entry['hallucinated_citation_rate']:.3f} | "
        f"{entry['decoy_resistance']:.2f} | "
        f"{abst} | {conf_sep} | {cost} |"
    )


def gates_summary(entry: dict[str, Any]) -> str:
    """The hard-gate verdict for this run, in plain words."""
    hallucinated = entry["hallucinated_citation_rate"] != m.GATE_HALLUCINATIONS_MAX
    coverage = entry["citation_coverage"] < m.GATE_COVERAGE_MIN
    precision = entry["precision_at_1"] < m.GATE_PRECISION_AT_1_MIN
    breached = [
        label
        for label, hit in (
            ("hallucinated citations", hallucinated),
            ("citation coverage", coverage),
            ("precision@1", precision),
        )
        if hit
    ]
    if breached:
        return f"**Gates FAILED:** {', '.join(breached)}."
    return (
        "All hard gates passed: hallucinated citations = 0, "
        "coverage ≥ 0.95, precision@1 ≥ 0.70."
    )


def build_block(latest: dict[str, Any]) -> str:
    return "\n".join(
        [
            MARKER_START,
            "Latest run (from `evals/results/history.jsonl`):",
            "",
            "| Run | Model | Commit | Cases | P@1 | R@3 | COV | HALL | "
            "DECOY | ABST | Conf.sep | Cost |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
            render_table(latest),
            "",
            gates_summary(latest),
            MARKER_END,
        ]
    )


def main() -> int:
    history = RESULTS_DIR / "history.jsonl"
    if not history.is_file():
        print(f"no history file at {history}", file=sys.stderr)
        return 1

    entries = [
        json.loads(line)
        for line in history.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not entries:
        print("history.jsonl is empty", file=sys.stderr)
        return 1
    latest = entries[-1]

    readme = README.read_text(encoding="utf-8")
    start = readme.find(MARKER_START)
    end = readme.find(MARKER_END)
    if start == -1 or end == -1:
        print("README.md is missing the EVAL-METRICS markers", file=sys.stderr)
        return 1

    block = build_block(latest)
    end_content = end + len(MARKER_END)
    readme = readme[:start] + block + readme[end_content:]
    README.write_text(readme, encoding="utf-8")
    print(f"README.md updated from {latest['timestamp']} ({latest['model']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
