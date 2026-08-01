"""Rebuild this incident's log fixture.

    python evals/incidents/inc-0008-false-positive/build_logs.py

Case 08 (docs/07): alert fired on load-test traffic. The errors all carry a
load-test marker and stop when the test stops. The generated `app.log` is
committed; this script only makes regeneration deterministic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _loggen import ErrorBurst, generate  # noqa: E402

START = datetime(2026, 4, 16, 16, 0, tzinfo=UTC)
END = datetime(2026, 4, 16, 17, 0, tzinfo=UTC)


def main() -> None:
    generate(
        Path(__file__).resolve().parent / "logs" / "app.log",
        start=START,
        end=END,
        services=("checkout-svc", "checkout-svc", "checkout-svc", "inventory-svc"),
        bursts=(
            ErrorBurst(
                timedelta(minutes=2),
                timedelta(minutes=20),
                "checkout-svc",
                ("load test request failed: expected {n} got {n}",),
                density=0.5,
            ),
        ),
    )
    return None


if __name__ == "__main__":
    main()
    sys.exit(0)
