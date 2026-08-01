"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0003-upstream-outage/build_repo.py

Case 03 (docs/07): upstream third-party outage, no internal change. The only
in-window commit is the decoy — an inventory template restyle that touches
nothing the failure mentions, and that lands *after* the outage starts, so the
candidate linker never links it to a symptom. The failure text (upstream
payment-gateway) is the evidence that the cause is outside the repo entirely.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 3, 26, 10, 0, tzinfo=UTC)
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

    # The decoy. In-window, plausible, touches nothing the failure names, and
    # lands after the outage is already under way.
    commit(
        "restyle inventory error pages",
        {"inventory/templates/error.py": "ERROR_TEMPLATE = 'error-v2'\n"},
        START + timedelta(minutes=20),
    )
    print(f"built {repo} with 1 commit")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
