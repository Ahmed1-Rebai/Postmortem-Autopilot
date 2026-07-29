"""Rebuild this incident's git fixture.

    python evals/incidents/inc-0007-recurrence-of-0001/build_repo.py

Recurrence of #01, six weeks later (docs/07, case 07). The failure shape is
deliberately the *same* as INC-0001's — a commit removes the connection pool
guard, the pool exhausts, the same alert fires — so the causal fingerprint
components (`cause_type|effect_type|service|signature`) match on the nose.
That match is what the case is actually testing: that recurrence memory
surfaces INC-0001 unprompted, and names whichever of its corrective actions
never landed.

`repo/` is generated, not committed — see `inc-0001-missing-env-var/build_repo.py`
for why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Six weeks after INC-0001's window (2026-03-12).
START = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
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

    # A different developer, six weeks later, reintroduces the same class of
    # bug: the pool-size guard that INC-0001's fix was supposed to add is
    # missing again — because it was never actually merged. That is the
    # story recurrence memory should be able to tell on its own.
    commit(
        "simplify checkout pool configuration",
        {
            "checkout/db/pool.py": "MAX_CONNECTIONS = None  # unbounded\n",
            "checkout/db/session.py": "def session():\n    return _pool.acquire()\n",
        },
        START + timedelta(seconds=50),
    )
    print(f"built {repo} with 1 commit")


if __name__ == "__main__":
    build(HERE / "repo")
    sys.exit(0)
