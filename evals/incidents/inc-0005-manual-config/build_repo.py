"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0005-manual-config/build_repo.py

Case 05 (docs/07): config change outside git. The failure's cause is a manual
`kubectl edit` that never produced a commit — so the repo genuinely contains
nothing that explains the cache outage. The one in-window commit is the decoy:
a checkout logging change that the failure text names nothing about, landing
after the outage starts.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 4, 2, 14, 0, tzinfo=UTC)
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

    # The decoy. Same service as the failing one, in-window, and it lands after
    # the failures — nothing the failure names refers to it.
    commit(
        "add request logging to checkout",
        {"checkout/middleware.py": "LOG_REQUESTS = True\n"},
        START + timedelta(minutes=20),
    )
    print(f"built {repo} with 1 commit")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
