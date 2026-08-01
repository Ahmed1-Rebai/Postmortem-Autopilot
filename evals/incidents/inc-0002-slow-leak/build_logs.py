"""Rebuild this incident's log fixture.

    python evals/incidents/inc-0002-slow-leak/build_logs.py

Case 02 (docs/07): slow leak. GC forced pauses escalate through most of the
window (the leading indicator), then OOM restarts begin near the end. The
generated `app.log` is committed; this script only makes regeneration
deterministic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _loggen import ErrorBurst, generate  # noqa: E402

START = datetime(2026, 3, 19, 9, 0, tzinfo=UTC)
END = datetime(2026, 3, 19, 10, 0, tzinfo=UTC)


def main() -> None:
    generate(
        Path(__file__).resolve().parent / "logs" / "app.log",
        start=START,
        end=END,
        services=("checkout-svc", "checkout-svc", "checkout-svc", "inventory-svc"),
        bursts=(
            ErrorBurst(
                timedelta(minutes=2),
                timedelta(minutes=40),
                "checkout-svc",
                ("gc forced pause {n}ms; heap size {n}mb",),
                density=0.7,
            ),
            ErrorBurst(
                timedelta(minutes=42),
                timedelta(minutes=55),
                "checkout-svc",
                ("out of memory; container restarting",),
                density=0.5,
            ),
        ),
    )
    return None


if __name__ == "__main__":
    main()
    sys.exit(0)
