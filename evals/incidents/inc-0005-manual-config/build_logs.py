"""Rebuild this incident's log fixture.

    python evals/incidents/inc-0005-manual-config/build_logs.py

Case 05 (docs/07): cache outages after a config change made outside git. The
logs show checkout failing to reach Redis; they contain no clue about which
commit did it, because none did. A single signature keeps the evidence
minimal. The generated `app.log` is committed; this script only makes
regeneration deterministic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _loggen import ErrorBurst, generate  # noqa: E402

START = datetime(2026, 4, 2, 14, 0, tzinfo=UTC)
END = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)


def main() -> None:
    generate(
        Path(__file__).resolve().parent / "logs" / "app.log",
        start=START,
        end=END,
        services=("checkout-svc", "checkout-svc", "checkout-svc", "inventory-svc"),
        bursts=(
            ErrorBurst(
                timedelta(minutes=3),
                timedelta(minutes=45),
                "checkout-svc",
                ("redis connection refused at 10.42.1.5:6379",),
                density=0.6,
            ),
        ),
    )
    return None


if __name__ == "__main__":
    main()
    sys.exit(0)
