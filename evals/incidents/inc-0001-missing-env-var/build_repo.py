"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0001-missing-env-var/build_repo.py

`repo/` is generated rather than committed: a nested `.git` inside a tracked
directory becomes a broken gitlink in the parent repo, and a checked-in
submodule for two synthetic commits is worse than a twenty-line script. The
logs and alerts *are* committed — they are plain text and worth reading in a
diff.

Deterministic: same commits, same paths, same authored timestamps every time,
so the content-derived event IDs are stable across machines and the golden
expectations keep meaning what they say.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)
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

    # The decoy: real, in-window, plausible, and not the cause. Without it the
    # case measures nothing — any system picks the only candidate.
    commit(
        "bump inventory page size to 50",
        {"inventory/views.py": "PAGE_SIZE = 50\n"},
        START + timedelta(seconds=20),
    )
    # The cause. `app/pool.py` is what makes change_path_overlap fire against
    # the "pool exhausted" signature.
    commit(
        "remove unused DB_POOL_MAX from settings",
        {
            "checkout/config/database.py": "DATABASES = {'default': {}}\n",
            "checkout/app/pool.py": "def acquire():\n    return _pool.get()\n",
        },
        START + timedelta(seconds=40),
    )
    print(f"built {repo} with 2 commits")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
