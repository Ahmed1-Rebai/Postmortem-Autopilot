"""Collectors, against real files on disk.

No mocking: a collector's whole job is reading a source, so a mocked source
tests nothing. Fixtures are written to `tmp_path` and read back.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.collectors.alerts import AlertCollector
from agent.collectors.base import CollectorError, RawRecord
from agent.collectors.git import GitCollector
from agent.collectors.logs import LogCollector, parse_timestamp
from agent.state import EventType, Window

START = datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)
WINDOW = Window(START, START + timedelta(hours=1))


# ---------------------------------------------------------------------------
# RawRecord contract
# ---------------------------------------------------------------------------
def test_raw_record_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="naive timestamp"):
        RawRecord(
            source="logs",
            native_id="x:1",
            type=EventType.LOG_ERROR,
            timestamp=datetime(2026, 3, 12, 14, 0),  # noqa: DTZ001
            message="boom",
        )


def test_raw_record_requires_a_native_id():
    """The native ID is half of the content-derived event ID; an empty one
    would make the event ID a function of the source alone."""
    with pytest.raises(ValueError, match="no native_id"):
        RawRecord(
            source="logs",
            native_id="",
            type=EventType.LOG_ERROR,
            timestamp=START,
            message="boom",
        )


# ---------------------------------------------------------------------------
# timestamp parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-12T14:02:11Z", datetime(2026, 3, 12, 14, 2, 11, tzinfo=UTC)),
        ("2026-03-12T14:02:11+00:00", datetime(2026, 3, 12, 14, 2, 11, tzinfo=UTC)),
        ("2026-03-12 14:02:11", datetime(2026, 3, 12, 14, 2, 11, tzinfo=UTC)),
    ],
)
def test_parse_timestamp_variants(raw: str, expected: datetime):
    assert parse_timestamp(raw) == expected


def test_parse_timestamp_assumes_utc_when_no_offset():
    """Stated explicitly because it is an assumption, not a fact about the log."""
    parsed = parse_timestamp("2026-03-12 14:02:11")
    assert parsed is not None
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize("bad", ["", "   ", "not a date", "12/03/2026", None, []])
def test_parse_timestamp_returns_none_rather_than_guessing(bad: object):
    assert parse_timestamp(bad) is None


# ---------------------------------------------------------------------------
# LogCollector
# ---------------------------------------------------------------------------
def write_log(tmp_path: Path, name: str, body: str) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return directory


def test_plain_log_lines_are_collected(tmp_path: Path):
    directory = write_log(
        tmp_path,
        "app.log",
        "2026-03-12T14:02:11Z ERROR [checkout] pool exhausted (32/32)\n"
        "2026-03-12T14:02:12Z INFO [checkout] request served\n"
        "2026-03-12T14:02:13Z ERROR [checkout] pool exhausted (64/64)\n",
    )
    records = LogCollector(directory).collect(WINDOW, None)

    assert len(records) == 2, "INFO lines must not become events"
    assert all(record.type is EventType.LOG_ERROR for record in records)
    assert records[0].service_hint == "checkout"
    assert records[0].message == "pool exhausted (32/32)"
    assert records[0].attributes["level"] == "error"


def test_json_lines_are_collected(tmp_path: Path):
    directory = write_log(
        tmp_path,
        "app.jsonl",
        json.dumps(
            {
                "timestamp": "2026-03-12T14:05:00Z",
                "level": "error",
                "message": "pool exhausted",
                "service": "checkout-svc",
            }
        )
        + "\n"
        + json.dumps(
            {"timestamp": "2026-03-12T14:06:00Z", "level": "info", "message": "ok"}
        )
        + "\n",
    )
    records = LogCollector(directory).collect(WINDOW, None)

    assert len(records) == 1
    assert records[0].service_hint == "checkout-svc"
    assert records[0].message == "pool exhausted"


def test_plain_and_json_lines_can_coexist(tmp_path: Path):
    directory = write_log(tmp_path, "plain.log", "2026-03-12T14:02:11Z ERROR boom\n")
    (directory / "structured.jsonl").write_text(
        json.dumps({"ts": "2026-03-12T14:03:00Z", "severity": "fatal", "msg": "worse"})
        + "\n",
        encoding="utf-8",
    )
    records = LogCollector(directory).collect(WINDOW, None)
    assert {record.message for record in records} == {"boom", "worse"}


def test_lines_outside_the_window_are_dropped(tmp_path: Path):
    directory = write_log(
        tmp_path,
        "app.log",
        "2026-03-12T13:00:00Z ERROR before the window\n"
        "2026-03-12T14:30:00Z ERROR inside the window\n"
        "2026-03-12T16:00:00Z ERROR after the window\n",
    )
    records = LogCollector(directory).collect(WINDOW, None)
    assert [record.message for record in records] == ["inside the window"]


def test_unparseable_lines_are_skipped_not_guessed(tmp_path: Path):
    """A misparsed timestamp would place an event outside its own incident."""
    directory = write_log(
        tmp_path,
        "app.log",
        "this line has no timestamp at all\n"
        "  \n"
        "{not valid json\n"
        "2026-03-12T14:02:11Z ERROR the only real one\n",
    )
    records = LogCollector(directory).collect(WINDOW, None)
    assert len(records) == 1


def test_service_filter_narrows_results(tmp_path: Path):
    directory = write_log(
        tmp_path,
        "app.log",
        "2026-03-12T14:02:11Z ERROR [checkout] boom\n"
        "2026-03-12T14:02:12Z ERROR [inventory] boom\n",
    )
    records = LogCollector(directory).collect(WINDOW, "checkout")
    assert len(records) == 1
    assert records[0].service_hint == "checkout"


def test_missing_log_directory_fails_loudly(tmp_path: Path):
    with pytest.raises(CollectorError, match="no log files"):
        LogCollector(tmp_path / "nonexistent").collect(WINDOW, None)


def test_log_collection_is_stable_across_runs(tmp_path: Path):
    directory = write_log(
        tmp_path, "app.log", "2026-03-12T14:02:11Z ERROR pool exhausted\n"
    )
    collector = LogCollector(directory)
    first = collector.collect(WINDOW, None)
    second = collector.collect(WINDOW, None)
    assert [r.native_id for r in first] == [r.native_id for r in second]


# ---------------------------------------------------------------------------
# AlertCollector
# ---------------------------------------------------------------------------
def write_alerts(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "HighErrorRate",
        "service": "checkout",
        "severity": "page",
    },
    "annotations": {"summary": "error rate above 5% for 5m"},
    "startsAt": "2026-03-12T14:02:11Z",
}


def test_alertmanager_envelope_is_read(tmp_path: Path):
    path = write_alerts(tmp_path, {"alerts": [ALERT]})
    records = AlertCollector(path).collect(WINDOW, None)

    assert len(records) == 1
    record = records[0]
    assert record.type is EventType.ALERT_FIRED
    assert record.service_hint == "checkout"
    assert record.attributes["rule_name"] == "HighErrorRate"
    assert record.attributes["alert_severity"] == "page"
    assert "error rate above" in record.message


def test_a_bare_list_of_alerts_is_also_accepted(tmp_path: Path):
    path = write_alerts(tmp_path, [ALERT])
    assert len(AlertCollector(path).collect(WINDOW, None)) == 1


def test_alert_native_id_is_stable(tmp_path: Path):
    """Re-collection must land on the same node, so the ID uses the rule name
    and start time, never a position in the file."""
    path = write_alerts(tmp_path, {"alerts": [ALERT]})
    collector = AlertCollector(path)
    first = collector.collect(WINDOW, None)[0]
    second = collector.collect(WINDOW, None)[0]
    assert first.native_id == second.native_id == "alert:HighErrorRate:1773324131"


def test_alerts_without_a_start_time_are_skipped(tmp_path: Path):
    path = write_alerts(tmp_path, {"alerts": [{**ALERT, "startsAt": None}]})
    assert AlertCollector(path).collect(WINDOW, None) == []


def test_alerts_without_a_name_are_skipped(tmp_path: Path):
    path = write_alerts(tmp_path, {"alerts": [{**ALERT, "labels": {}}]})
    assert AlertCollector(path).collect(WINDOW, None) == []


def test_extra_labels_are_flattened_onto_the_event(tmp_path: Path):
    alert = {**ALERT, "labels": {**ALERT["labels"], "cluster": "prod-eu"}}
    path = write_alerts(tmp_path, {"alerts": [alert]})
    record = AlertCollector(path).collect(WINDOW, None)[0]
    assert record.attributes["label_cluster"] == "prod-eu"


def test_missing_alerts_fixture_fails_loudly(tmp_path: Path):
    with pytest.raises(CollectorError, match="not found"):
        AlertCollector(tmp_path / "nope.json").collect(WINDOW, None)


def test_malformed_alerts_fixture_fails_loudly(tmp_path: Path):
    path = tmp_path / "alerts.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CollectorError, match="cannot read"):
        AlertCollector(path).collect(WINDOW, None)


# ---------------------------------------------------------------------------
# GitCollector
# ---------------------------------------------------------------------------
def make_repo(tmp_path: Path, commits: list[tuple[str, dict[str, str]]]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")

    when = START + timedelta(minutes=5)
    for subject, files in commits:
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        run("add", "-A")
        stamp = when.isoformat()
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", subject],
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_DATE": stamp,
                "GIT_COMMITTER_DATE": stamp,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        )
        when += timedelta(minutes=1)
    return repo


def test_git_collector_captures_files_changed(tmp_path: Path):
    """`files_changed` feeds change_path_overlap, the 0.30-weight signal."""
    repo = make_repo(
        tmp_path,
        [
            (
                "remove DB_POOL_MAX from settings",
                {
                    "checkout/config/database.py": "POOL=None\n",
                    "checkout/app/pool.py": "pass\n",
                },
            )
        ],
    )
    records = GitCollector(repo).collect(WINDOW, None)

    assert len(records) == 1
    record = records[0]
    assert record.type is EventType.COMMIT
    assert record.message == "remove DB_POOL_MAX from settings"
    assert set(record.attributes["files_changed"]) == {  # type: ignore[arg-type]
        "checkout/config/database.py",
        "checkout/app/pool.py",
    }
    assert record.service_hint == "checkout"
    assert record.native_id.startswith("commit:")


def test_commit_spanning_two_services_infers_neither(tmp_path: Path):
    """Asserting one would hand the linker a false overlap."""
    repo = make_repo(
        tmp_path,
        [("touch both", {"checkout/a.py": "x\n", "inventory/b.py": "y\n"})],
    )
    record = GitCollector(repo).collect(WINDOW, None)[0]
    assert record.service_hint is None


def test_commits_outside_the_window_are_excluded(tmp_path: Path):
    repo = make_repo(tmp_path, [("in window", {"checkout/a.py": "x\n"})])
    far_window = Window(START + timedelta(days=1), START + timedelta(days=2))
    assert GitCollector(repo).collect(far_window, None) == []


def test_git_collector_timestamps_are_aware(tmp_path: Path):
    repo = make_repo(tmp_path, [("a commit", {"checkout/a.py": "x\n"})])
    record = GitCollector(repo).collect(WINDOW, None)[0]
    assert record.timestamp.tzinfo is not None


def test_git_collection_is_stable_across_runs(tmp_path: Path):
    repo = make_repo(tmp_path, [("a commit", {"checkout/a.py": "x\n"})])
    collector = GitCollector(repo)
    assert [r.native_id for r in collector.collect(WINDOW, None)] == [
        r.native_id for r in collector.collect(WINDOW, None)
    ]


def test_non_repository_fails_loudly(tmp_path: Path):
    with pytest.raises(CollectorError):
        GitCollector(tmp_path / "not-a-repo").collect(WINDOW, None)
