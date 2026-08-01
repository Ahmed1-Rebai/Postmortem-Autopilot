"""Shared deterministic generator for golden-incident `logs/app.log`.

Each incident commits its generated `app.log` — plain text, worth reading in
a diff, same rationale as `build_repo.py` — and this module is what makes
regenerating it deterministic rather than hand-rolled. A per-incident
`build_logs.py` describes only that incident's story (window, services, error
bursts) and calls `generate`.

The collector keeps only ERROR-level lines, so the INFO traffic is noise that
exercises the signature-collapsing path; error bursts are what become events.
Messages use `{n}` for the numbers that vary between occurrences, so repeated
lines in a burst collapse onto one signature with a real count.

Deterministic on a fixed `seed`: same window, same bursts, same output, so
regenerating the file cannot drift the content-derived event IDs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_INFO_LINES: tuple[str, ...] = (
    "request served in {n}ms",
    "healthz ok in {n}ms",
    "batch {n} processed",
)

_MS: tuple[int, ...] = (
    3, 5, 8, 12, 16, 21, 26, 34, 42, 49, 53, 58, 64, 71, 79, 84, 90, 95, 99,
)


@dataclass(frozen=True, slots=True)
class ErrorBurst:
    """One failure mode, active for part of the window."""

    start_offset: timedelta
    end_offset: timedelta
    service: str
    templates: tuple[str, ...]
    #: Chance that a second inside the burst emits an error line instead of
    #: INFO noise. High enough to produce a real event count, low enough that
    #: the log still mostly reads like traffic.
    density: float = 0.6


def generate(
    path: Path,
    *,
    start: datetime,
    end: datetime,
    services: tuple[str, ...],
    bursts: tuple[ErrorBurst, ...],
    seed: int = 0,
) -> None:
    """Write a deterministic `app.log` covering `[start, end)`."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    rng = random.Random(seed)
    counters: dict[str, int] = {}

    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    cursor = start
    while cursor < end:
        offset = cursor - start
        burst = _burst_at(bursts, offset)
        if burst is not None and rng.random() < burst.density:
            counters[burst.service] = counters.get(burst.service, 0) + 1
            template = rng.choice(burst.templates)
            message = _fill(template, counters[burst.service])
            lines.append(
                f"{_fmt(cursor)} ERROR [{burst.service}] {message}"
            )
        else:
            service = rng.choice(services)
            info = rng.choice(_INFO_LINES)
            lines.append(f"{_fmt(cursor)} INFO [{service}] {_fill(info, rng.choice(_MS))}")
        cursor += timedelta(seconds=1)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path} with {len(lines)} lines")


def _burst_at(bursts: tuple[ErrorBurst, ...], offset: timedelta) -> ErrorBurst | None:
    """The most recently *started* burst that is active, or None.

    Cascading failures overlap: inventory keeps failing while checkout starts
    timing out on top of it. Picking the latest-started active burst is what
    staggers the three signatures correctly instead of letting the first
    burst shadow the later ones for its whole duration.
    """
    active = [b for b in bursts if b.start_offset <= offset < b.end_offset]
    return max(active, key=lambda b: b.start_offset, default=None)


def _fill(template: str, value: int) -> str:
    return template.replace("{n}", str(value))


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
