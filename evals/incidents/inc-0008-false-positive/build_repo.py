"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0008-false-positive/build_repo.py

Case 08 (docs/07): alert fired on load-test traffic, not a real incident. The
one in-window commit is the decoy — a checkout queue-limit change that the
failure text names nothing about, landing after the test traffic stops.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 4, 16, 16, 0, tzinfo=UTC)
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

    # The decoy. Same service as the alert, in-window, unrelated to the
    # failure text, and landing after the load-test traffic has stopped.
    commit(
        "tune checkout request queue limits",
        {"checkout/queue.py": "MAX_QUEUE_DEPTH = 500\n"},
        START + timedelta(minutes=25),
    )
    print(f"built {repo} with 1 commit")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
