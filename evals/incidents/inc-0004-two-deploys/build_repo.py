"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0004-two-deploys/build_repo.py

The discrimination case. Two changes land 40 seconds apart, **both to the
checkout service**, and only one is causal. That is deliberate: when both
changes are under `checkout/`, service overlap fires for both and so does the
path-segment half of change-path overlap. The only thing that separates them is
the module the failure actually names — `pool.py` against "connection pool
exhausted", versus `receipt.py` and `mailer.py` against nothing.

If a run blames the templates change, the system is pattern-matching on "a
change happened near the failure" and the strong signal is not doing its job.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
HERE = Path(__file__).resolve().parent


def build(repo: Path) -> None:
    if repo.exists():
        print(f"{repo} already exists — remove it to rebuild")
        return
    repo.mkdir(parents=True)
    env = {**os.environ}

    def git(*args: str, **overrides: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env={**env, **overrides},
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "Dev")

    def commit(subject: str, files: dict[str, str], at: datetime) -> None:
        for name, body in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        git("add", "-A")
        stamp = at.isoformat()
        git(
            "commit",
            "-q",
            "-m",
            subject,
            GIT_AUTHOR_DATE=stamp,
            GIT_COMMITTER_DATE=stamp,
        )

    # The decoy. Same service, in-window, lands *first* so temporal proximity
    # favours it, and touches nothing the failure mentions.
    commit(
        "restyle checkout receipt emails",
        {
            "checkout/templates/receipt.py": "TEMPLATE = 'receipt-v2'\n",
            "checkout/mailer.py": "def send_receipt(order):\n    ...\n",
        },
        START + timedelta(seconds=30),
    )
    # The cause. Same service again — only `pool.py` connects it to the
    # failure text.
    commit(
        "reduce checkout db connection pool size",
        {
            "checkout/db/pool.py": "MAX_CONNECTIONS = 4\n",
            "checkout/db/session.py": "def session():\n    return _pool.acquire()\n",
        },
        START + timedelta(seconds=70),
    )
    print(f"built {repo} with 2 commits")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
