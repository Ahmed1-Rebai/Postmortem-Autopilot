"""Rebuild this incident's log fixture.

    python evals/incidents/inc-0003-upstream-outage/build_logs.py

Case 03 (docs/07): upstream provider outage. Checkout logs only one failure
mode — gateway errors — with no internal failure and no code change to blame.
A single signature keeps the evidence minimal: the incident is one collapsed
failure plus one alert. The generated `app.log` is committed; this script only
makes regeneration deterministic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _loggen import ErrorBurst, generate  # noqa: E402

START = datetime(2026, 3, 26, 10, 0, tzinfo=UTC)
END = datetime(2026, 3, 26, 11, 0, tzinfo=UTC)


def main() -> None:
    generate(
        Path(__file__).resolve().parent / "logs" / "app.log",
        start=START,
        end=END,
        services=("checkout-svc", "checkout-svc", "checkout-svc", "inventory-svc"),
        bursts=(
            ErrorBurst(
                timedelta(minutes=1, seconds=30),
                timedelta(minutes=50),
                "checkout-svc",
                ("upstream payment-gateway returned 503 after {n}ms",),
                density=0.6,
            ),
        ),
    )
    return None


if __name__ == "__main__":
    main()
    sys.exit(0)
