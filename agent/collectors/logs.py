"""Log collector — plain text and JSON lines from disk.

Phase 1 reads files; Phase 4 swaps in Loki's `query_range`. The `RawRecord`
shape is what makes that a drop-in change.

Only error-level lines become events. That is not a shortcut: a postmortem
reasons about failures, and admitting every INFO line would bury the signal and
blow up the graph without adding one causal hypothesis.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from agent.collectors.base import CollectorError, RawRecord
from agent.state import EventType, Window

#: Levels that count as a failure worth reasoning about.
ERROR_LEVELS: Final[frozenset[str]] = frozenset(
    {"error", "err", "fatal", "critical", "crit", "severe", "panic"}
)

_JSON_TIME_KEYS: Final[tuple[str, ...]] = ("timestamp", "ts", "time", "@timestamp")
_JSON_MESSAGE_KEYS: Final[tuple[str, ...]] = ("message", "msg", "log", "event")
_JSON_LEVEL_KEYS: Final[tuple[str, ...]] = ("level", "severity", "lvl")
_JSON_SERVICE_KEYS: Final[tuple[str, ...]] = (
    "service",
    "service_name",
    "app",
    "logger",
)

#: `2026-03-12T14:02:11.482Z ERROR message` and near neighbours. Anything this
#: does not match is skipped rather than guessed at — a misparsed timestamp
#: would place an event outside its own incident window.
_PLAIN_LINE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)\s+"
    r"\[?(?P<level>[A-Za-z]+)\]?\s*:?\s+"
    r"(?P<rest>.*)$"
)

#: An optional `[service]` prefix on the message body.
_SERVICE_PREFIX: Final[re.Pattern[str]] = re.compile(r"^\[(?P<service>[\w.\-]+)\]\s*")


def parse_timestamp(value: object) -> datetime | None:
    """Parse a log timestamp into an aware datetime, or None if unparseable.

    A naive timestamp is assumed UTC — log files routinely omit the offset, and
    refusing to read them would make the collector useless. The assumption is
    recorded here rather than silently applied downstream.
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class LogCollector:
    """Reads `.log`, `.jsonl` and `.json` files under a directory."""

    name = "logs"

    def __init__(self, path: Path, source: str = "logs") -> None:
        self._path = path
        self.source = source

    def collect(self, window: Window, service: str | None) -> Sequence[RawRecord]:
        files = self._files()
        if not files:
            raise CollectorError(f"no log files found under {self._path}")

        records: list[RawRecord] = []
        for file in files:
            try:
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                raise CollectorError(f"cannot read {file}: {exc}") from exc

            for line_number, line in enumerate(lines, start=1):
                record = self._parse_line(file, line_number, line)
                if record is None:
                    continue
                if not window.contains(record.timestamp):
                    continue
                if service and record.service_hint and record.service_hint != service:
                    continue
                records.append(record)
        return records

    # -- internals ----------------------------------------------------------
    def _files(self) -> list[Path]:
        if self._path.is_file():
            return [self._path]
        if not self._path.is_dir():
            return []
        return sorted(
            candidate
            for pattern in ("*.log", "*.jsonl", "*.json", "*.txt")
            for candidate in self._path.rglob(pattern)
        )

    def _parse_line(self, file: Path, line_number: int, line: str) -> RawRecord | None:
        if not line.strip():
            return None
        stripped = line.lstrip()
        if stripped.startswith("{"):
            return self._parse_json_line(file, line_number, stripped)
        return self._parse_plain_line(file, line_number, line)

    def _parse_json_line(
        self, file: Path, line_number: int, line: str
    ) -> RawRecord | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None

        level = str(_first(payload, _JSON_LEVEL_KEYS) or "").lower()
        if level not in ERROR_LEVELS:
            return None
        timestamp = parse_timestamp(_first(payload, _JSON_TIME_KEYS))
        message = _first(payload, _JSON_MESSAGE_KEYS)
        if timestamp is None or not message:
            return None

        service_hint = _first(payload, _JSON_SERVICE_KEYS)
        return self._record(
            file=file,
            line_number=line_number,
            timestamp=timestamp,
            message=str(message),
            level=level,
            service_hint=str(service_hint) if service_hint else None,
            raw=payload,
        )

    def _parse_plain_line(
        self, file: Path, line_number: int, line: str
    ) -> RawRecord | None:
        match = _PLAIN_LINE.match(line)
        if match is None:
            return None
        level = match.group("level").lower()
        if level not in ERROR_LEVELS:
            return None
        timestamp = parse_timestamp(match.group("ts"))
        if timestamp is None:
            return None

        rest = match.group("rest")
        service_hint: str | None = None
        prefix = _SERVICE_PREFIX.match(rest)
        if prefix is not None:
            service_hint = prefix.group("service")
            rest = rest[prefix.end() :]

        return self._record(
            file=file,
            line_number=line_number,
            timestamp=timestamp,
            message=rest.strip(),
            level=level,
            service_hint=service_hint,
            raw={"line": line, "file": file.name},
        )

    def _record(
        self,
        *,
        file: Path,
        line_number: int,
        timestamp: datetime,
        message: str,
        level: str,
        service_hint: str | None,
        raw: dict[str, Any],
    ) -> RawRecord:
        # File and line number are stable across re-collection of an unchanged
        # file. The normalizer re-keys log errors onto their signature anyway,
        # so this only has to be unique within a run.
        return RawRecord(
            source=self.source,
            native_id=f"{file.name}:{line_number}",
            type=EventType.LOG_ERROR,
            timestamp=timestamp,
            message=message,
            service_hint=service_hint,
            attributes={"level": level},
            raw=raw,
        )


def _first(payload: dict[str, Any], keys: Sequence[str]) -> Any | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None
