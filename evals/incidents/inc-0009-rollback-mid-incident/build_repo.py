"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0009-rollback-mid-incident/build_repo.py

Case 09 (docs/07): a misconfig is deployed, fails, and is reverted
mid-incident. The revert is a real `Revert '...'` commit whose subject
fingerprints to `revert <str>` — it shares no token with the failure text, so
`change_path_overlap` and `log_signature_match` only ever point at the
original misconfig commit, never at the revert.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 4, 30, 10, 0, tzinfo=UTC)
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

    # The cause. Touches the code the failure text names ("retries").
    commit(
        "enable aggressive checkout retries",
        {"checkout/retry.py": "MAX_ATTEMPTS = 10\nBACKOFF_MS = 50\n"},
        START + timedelta(minutes=4),
    )
    # The decoy. In-window, unrelated to the failure.
    commit(
        "bump inventory batch size",
        {"inventory/batch.py": "BATCH_SIZE = 200\n"},
        START + timedelta(minutes=6),
    )
    # The rollback. Mid-incident, and the errors stop the minute it lands.
    commit(
        "Revert 'enable aggressive checkout retries'",
        {"checkout/retry.py": "MAX_ATTEMPTS = 3\nBACKOFF_MS = 200\n"},
        START + timedelta(minutes=13),
    )
    print(f"built {repo} with 3 commits")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
