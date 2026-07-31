"""`expected.yaml`'s pseudo-IDs resolved against real, content-derived
`Event`s — the mapping the eval harness's precision/recall/must-cite metrics
all depend on being correct.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent.state import Event, EventType, make_event_id
from evals.labels import (
    UnresolvedLabelError,
    resolve_label,
    resolve_labels,
    resolved_ids,
)

START = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)


def a_commit(summary: str, event_id: str = "c1") -> Event:
    return Event(
        id=event_id,
        type=EventType.COMMIT,
        timestamp=START,
        source="git",
        summary=summary,
    )


def an_alert(rule_name: str, event_id: str = "a1") -> Event:
    return Event(
        id=event_id,
        type=EventType.ALERT_FIRED,
        timestamp=START,
        source="alerts",
        summary=f"{rule_name}: something happened",
        attributes={"rule_name": rule_name},
    )


def a_log_error(signature: str, event_id: str = "l1") -> Event:
    return Event(
        id=event_id,
        type=EventType.LOG_ERROR,
        timestamp=START,
        source="logs",
        summary="ConnectionError: pool exhausted (32/32)",
        signature=signature,
    )


# ---------------------------------------------------------------------------
# commit: prefix
# ---------------------------------------------------------------------------
def test_commit_resolves_by_exact_subject_case_insensitive():
    commit = a_commit("Remove unused DB_POOL_MAX from settings")
    found = resolve_label("commit:remove unused db_pool_max from settings", [commit])
    assert found == [commit]


def test_commit_does_not_match_a_different_subject():
    commit = a_commit("bump inventory page size to 50")
    with pytest.raises(UnresolvedLabelError):
        resolve_label("commit:remove unused DB_POOL_MAX from settings", [commit])


def test_commit_does_not_match_a_non_commit_event():
    alert = an_alert("HighErrorRate")
    with pytest.raises(UnresolvedLabelError):
        resolve_label("commit:HighErrorRate", [alert])


# ---------------------------------------------------------------------------
# alert: prefix
# ---------------------------------------------------------------------------
def test_alert_resolves_by_exact_rule_name():
    alert = an_alert("HighErrorRate")
    assert resolve_label("alert:HighErrorRate", [alert]) == [alert]


def test_alert_rule_name_match_is_case_sensitive():
    """Alertmanager rule names are exact identifiers, not prose — unlike
    commit subjects, a case mismatch here is a real fixture typo worth
    surfacing, not something to paper over."""
    alert = an_alert("HighErrorRate")
    with pytest.raises(UnresolvedLabelError):
        resolve_label("alert:higherrorrate", [alert])


# ---------------------------------------------------------------------------
# log:sig: prefix
# ---------------------------------------------------------------------------
def test_log_sig_resolves_by_exact_signature():
    log = a_log_error("connectionerror: pool exhausted (<num>/<num>)")
    found = resolve_label(
        "log:sig:connectionerror: pool exhausted (<num>/<num>)", [log]
    )
    assert found == [log]


def test_log_sig_does_not_match_a_different_signature():
    log = a_log_error("timeouterror: upstream unreachable")
    with pytest.raises(UnresolvedLabelError):
        resolve_label("log:sig:connectionerror: pool exhausted (<num>/<num>)", [log])


# ---------------------------------------------------------------------------
# unrecognized prefix / unresolved fixture bug
# ---------------------------------------------------------------------------
def test_unrecognized_prefix_raises():
    with pytest.raises(UnresolvedLabelError):
        resolve_label("deploy:v2.3.1", [a_commit("anything")])


def test_empty_event_list_raises():
    with pytest.raises(UnresolvedLabelError):
        resolve_label("commit:anything", [])


# ---------------------------------------------------------------------------
# resolve_labels / resolved_ids
# ---------------------------------------------------------------------------
def test_resolve_labels_flattens_across_multiple_pseudo_ids():
    commit = a_commit("remove unused DB_POOL_MAX from settings", event_id="c1")
    alert = an_alert("HighErrorRate", event_id="a1")
    found = resolve_labels(
        ["commit:remove unused DB_POOL_MAX from settings", "alert:HighErrorRate"],
        [commit, alert],
    )
    assert found == [commit, alert]


def test_resolve_labels_raises_on_the_first_unresolved_one():
    commit = a_commit("remove unused DB_POOL_MAX from settings")
    with pytest.raises(UnresolvedLabelError):
        resolve_labels(
            ["commit:remove unused DB_POOL_MAX from settings", "alert:NoSuchAlert"],
            [commit],
        )


def test_resolved_ids_deduplicates_real_ids():
    same_commit_twice = a_commit(
        "remove unused DB_POOL_MAX from settings", event_id="c1"
    )
    ids = resolved_ids(
        [
            "commit:remove unused DB_POOL_MAX from settings",
            "commit:remove unused DB_POOL_MAX from settings",
        ],
        [same_commit_twice],
    )
    assert ids == frozenset({"c1"})


def test_resolved_ids_matches_real_content_derived_ids():
    """Not a special test-only ID scheme — the same `make_event_id` the
    pipeline itself uses."""
    real_id = make_event_id("git", "commit:abc123def456")
    commit = a_commit("remove unused DB_POOL_MAX from settings", event_id=real_id)
    ids = resolved_ids(["commit:remove unused DB_POOL_MAX from settings"], [commit])
    assert ids == frozenset({real_id})
