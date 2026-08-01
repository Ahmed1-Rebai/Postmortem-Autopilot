"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0002-slow-leak/build_repo.py

Case 02 (docs/07): slow leak, cause outside the window. The causal commit was
authored six hours before the incident window and is deliberately NOT
collected — that is the point of the case. The only in-window commit is the
decoy, a payment-gateway retry-timeout change that explains none of the
symptoms.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 3, 19, 9, 0, tzinfo=UTC)
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

    # The real cause — six hours before the window, so no collector ever sees
    # it. Present in history for realism, absent from the evidence.
    commit(
        "increase checkout connection cache TTL",
        {"checkout/cache.py": "CONNECTION_CACHE_TTL = 3600\n"},
        START - timedelta(hours=6),
    )
    # The decoy. In-window, same service, unrelated to the symptoms.
    commit(
        "bump payment-gateway retry timeout",
        {"checkout/retry.py": "UPSTREAM_RETRY_TIMEOUT = 5\n"},
        START + timedelta(minutes=6),
    )
    print(f"built {repo} with 2 commits (1 outside the incident window)")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
