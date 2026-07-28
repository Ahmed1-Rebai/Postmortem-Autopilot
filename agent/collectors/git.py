"""Git collector — commits in the window, with the files each one touched.

`files_changed` is the payload that matters. It feeds `change_path_overlap`,
the 0.30-weight signal and the strongest one the model has: it is what
separates "a deploy happened around then" from "*this* deploy touched the code
that failed".
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from agent.collectors.base import CollectorError, RawRecord
from agent.state import EventType, Window

#: ASCII record/unit separators. Commit subjects contain newlines, quotes and
#: every other delimiter someone might reach for; these two do not appear in
#: practice, so parsing stays unambiguous.
_RECORD_SEP: Final[str] = "\x1e"
_FIELD_SEP: Final[str] = "\x1f"

_PRETTY: Final[str] = f"{_RECORD_SEP}%H{_FIELD_SEP}%aI{_FIELD_SEP}%an{_FIELD_SEP}%s"

_TIMEOUT_SECONDS: Final[int] = 30


class GitCollector:
    """Reads a local repository via `git log`."""

    name = "git"

    def __init__(self, repo_path: Path, source: str = "git") -> None:
        self._repo = repo_path
        self.source = source

    def collect(self, window: Window, service: str | None) -> Sequence[RawRecord]:
        if not (self._repo / ".git").exists() and not self._repo.is_dir():
            raise CollectorError(f"{self._repo} is not a git repository")

        output = self._run_git(window)
        return [
            record
            for chunk in output.split(_RECORD_SEP)
            if chunk.strip()
            if (record := self._parse_commit(chunk, service)) is not None
        ]

    # -- internals ----------------------------------------------------------
    def _run_git(self, window: Window) -> str:
        command = [
            "git",
            "-C",
            str(self._repo),
            "log",
            f"--since={window.start.isoformat()}",
            f"--until={window.end.isoformat()}",
            "--name-only",
            "--no-merges",
            f"--pretty=format:{_PRETTY}",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CollectorError(f"git log failed in {self._repo}: {exc}") from exc

        if completed.returncode != 0:
            raise CollectorError(
                f"git log failed in {self._repo}: {completed.stderr.strip()}"
            )
        return completed.stdout

    def _parse_commit(self, chunk: str, service: str | None) -> RawRecord | None:
        header, _, body = chunk.partition("\n")
        fields = header.split(_FIELD_SEP)
        if len(fields) < 4:
            return None
        sha, authored_at, author, subject = (
            fields[0].strip(),
            fields[1].strip(),
            fields[2].strip(),
            fields[3].strip(),
        )
        if not sha:
            return None

        try:
            timestamp = datetime.fromisoformat(authored_at)
        except ValueError:
            return None
        if timestamp.tzinfo is None:
            # %aI is always offset-qualified; a naive value means the format
            # changed underneath us, and guessing a zone would misplace the
            # commit in the timeline.
            return None

        files = tuple(line.strip() for line in body.splitlines() if line.strip())
        if service and files and not _touches_service(files, service):
            return None

        return RawRecord(
            source=self.source,
            native_id=f"commit:{sha[:12]}",
            type=EventType.COMMIT,
            timestamp=timestamp,
            message=subject,
            service_hint=_infer_service(files),
            attributes={
                "sha": sha,
                "short_sha": sha[:12],
                "author": author,
                "files_changed": files,
                "file_count": len(files),
            },
            raw={"sha": sha, "subject": subject, "files": list(files)},
        )


def _touches_service(files: Sequence[str], service: str) -> bool:
    needle = service.lower()
    return any(needle in path.lower() for path in files)


def _infer_service(files: Sequence[str]) -> str | None:
    """Guess the service from the top-level directory the commit touched.

    Only when *every* file agrees. A commit spanning two services has no single
    owning service, and asserting one would hand the linker a false overlap —
    the canonicalizer would then happily match it against the wrong incident.
    """
    tops = {path.split("/")[0] for path in files if "/" in path}
    if len(tops) == 1:
        return tops.pop()
    return None
