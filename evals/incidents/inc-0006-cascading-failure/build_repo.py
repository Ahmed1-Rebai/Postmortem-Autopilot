"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0006-cascading-failure/build_repo.py

Case 06 (docs/07): cascading failure across three services. The cause lands
first and touches `inventory/db/product.py`, whose file stem (`product`) is
named by the failing signature (`... executing product query`) — that is what
fires `change_path_overlap`, the signal that separates it from the decoy. The
decoy touches `checkout/session.py`, a file nothing in the failure text names.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 4, 9, 11, 0, tzinfo=UTC)
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

    # The cause. `product.py` is what connects it to the failure text.
    commit(
        "drop product query index from inventory",
        {"inventory/db/product.py": "INDEX = None  # removed\n"},
        START + timedelta(minutes=1),
    )
    # The decoy. Same window, same service as two of the three failing
    # services, but no file stem the failure text names.
    commit(
        "bump checkout session timeout",
        {"checkout/session.py": "SESSION_TIMEOUT = 900\n"},
        START + timedelta(minutes=2),
    )
    print(f"built {repo} with 2 commits")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
