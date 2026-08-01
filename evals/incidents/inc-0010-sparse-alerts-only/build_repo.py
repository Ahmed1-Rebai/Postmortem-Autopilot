"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0010-sparse-alerts-only/build_repo.py

Case 10 (docs/07): sparse, alerts-only evidence. The two in-window commits are
both decoys — unrelated checkout changes that land *after* the alert, so the
candidate linker never proposes them as causes. Nothing in the failure text
names them.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 5, 7, 13, 0, tzinfo=UTC)
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

    # Two decoys. Both after the alert, both under checkout, neither named by
    # anything in the evidence.
    commit(
        "bump checkout queue depth",
        {"checkout/queue.py": "MAX_QUEUE_DEPTH = 500\n"},
        START + timedelta(minutes=6),
    )
    commit(
        "refactor checkout order summary",
        {"checkout/summary.py": "SUMMARY = 'order-v2'\n"},
        START + timedelta(minutes=9),
    )
    print(f"built {repo} with 2 commits")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
