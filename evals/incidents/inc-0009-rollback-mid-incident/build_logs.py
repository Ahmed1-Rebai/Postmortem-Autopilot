"""Rebuild this incident's log fixture.

    python evals/incidents/inc-0009-rollback-mid-incident/build_logs.py

Case 09 (docs/07): errors begin ~90s after the misconfig deploy and stop the
minute the revert lands. The generated `app.log` is committed; this script
only makes regeneration deterministic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _loggen import ErrorBurst, generate  # noqa: E402

START = datetime(2026, 4, 30, 10, 0, tzinfo=UTC)
END = datetime(2026, 4, 30, 11, 0, tzinfo=UTC)


def main() -> None:
    generate(
        Path(__file__).resolve().parent / "logs" / "app.log",
        start=START,
        end=END,
        services=("checkout-svc", "checkout-svc", "checkout-svc", "inventory-svc"),
        bursts=(
            ErrorBurst(
                timedelta(minutes=5, seconds=30),
                timedelta(minutes=13),
                "checkout-svc",
                ("checkout retry policy exhausted after {n} attempts",),
                density=0.6,
            ),
        ),
    )
    return None


if __name__ == "__main__":
    main()
    sys.exit(0)
