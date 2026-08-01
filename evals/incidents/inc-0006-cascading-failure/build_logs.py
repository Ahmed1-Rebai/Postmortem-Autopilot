"""Rebuild this incident's log fixture.

    python evals/incidents/inc-0006-cascading-failure/build_logs.py

Case 06 (docs/07): cascading failure across three services. Inventory times
out on database queries first; checkout then times out calling inventory;
payment failures follow on top of checkout. Each hop is one collapsed
signature. The generated `app.log` is committed; this script only makes
regeneration deterministic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _loggen import ErrorBurst, generate  # noqa: E402

START = datetime(2026, 4, 9, 11, 0, tzinfo=UTC)
END = datetime(2026, 4, 9, 12, 0, tzinfo=UTC)


def main() -> None:
    generate(
        Path(__file__).resolve().parent / "logs" / "app.log",
        start=START,
        end=END,
        services=(
            "inventory-svc",
            "inventory-svc",
            "checkout-svc",
            "checkout-svc",
            "payment-gateway",
        ),
        bursts=(
            ErrorBurst(
                timedelta(minutes=2, seconds=30),
                timedelta(minutes=40),
                "inventory-svc",
                ("database timeout after {n}ms executing product query",),
                density=0.6,
            ),
            ErrorBurst(
                timedelta(minutes=3, seconds=30),
                timedelta(minutes=40),
                "checkout-svc",
                ("upstream inventory-svc timed out after {n}ms",),
                density=0.6,
            ),
            ErrorBurst(
                timedelta(minutes=4, seconds=30),
                timedelta(minutes=40),
                "payment-gateway",
                ("checkout request failed: upstream error",),
                density=0.5,
            ),
        ),
    )
    return None


if __name__ == "__main__":
    main()
    sys.exit(0)
