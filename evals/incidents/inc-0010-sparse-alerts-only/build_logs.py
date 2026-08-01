"""Rebuild this incident's log fixture.

    python evals/incidents/inc-0010-sparse-alerts-only/build_logs.py

Case 10 (docs/07): sparse, alerts-only evidence. The logs contain no error
lines at all — only routine INFO traffic — so the alert is the sole evidence
of an incident. The generated `app.log` is committed; this script only makes
regeneration deterministic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _loggen import generate  # noqa: E402

START = datetime(2026, 5, 7, 13, 0, tzinfo=UTC)
END = datetime(2026, 5, 7, 14, 0, tzinfo=UTC)


def main() -> None:
    generate(
        Path(__file__).resolve().parent / "logs" / "app.log",
        start=START,
        end=END,
        services=("checkout-svc", "checkout-svc", "checkout-svc", "inventory-svc"),
        bursts=(),
    )
    return None


if __name__ == "__main__":
    main()
    sys.exit(0)
