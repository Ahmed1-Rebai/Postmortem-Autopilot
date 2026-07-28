"""The linker: fixture event list → the exact candidate edge set.

docs/06 puts off-by-one window bugs here, so the window boundaries are tested
on both sides. The other thing tested hard is `change_path_overlap` — it is the
0.30-weight signal, the one that separates "a deploy happened" from "*this*
deploy", and a false positive there is worth more than a false negative
anywhere else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.linker import CandidateLinker, LinkerConfig, summarize
from agent.normalize.services import ServiceCanonicalizer
from agent.state import Event, EventType

START = datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return START + timedelta(seconds=seconds)


def commit(
    event_id: str,
    seconds: int,
    files: tuple[str, ...],
    service: str | None = "checkout",
) -> Event:
    return Event(
        id=event_id,
        type=EventType.COMMIT,
        timestamp=at(seconds),
        source="git",
        summary="a commit",
        service=service,
        attributes={"files_changed": files},
    )


def log_error(
    event_id: str,
    seconds: int,
    summary: str = "ConnectionError: pool exhausted",
    service: str | None = "checkout",
    signature: str | None = "connectionerror: pool exhausted",
) -> Event:
    return Event(
        id=event_id,
        type=EventType.LOG_ERROR,
        timestamp=at(seconds),
        source="logs",
        summary=summary,
        service=service,
        signature=signature,
    )


def alert(event_id: str, seconds: int, service: str | None = "checkout") -> Event:
    return Event(
        id=event_id,
        type=EventType.ALERT_FIRED,
        timestamp=at(seconds),
        source="alertmanager",
        summary="HighErrorRate firing",
        service=service,
        signature="higherrorrate firing",
    )


@pytest.fixture
def linker() -> CandidateLinker:
    canonicalizer = ServiceCanonicalizer.from_config(
        {"checkout": ["co"]}, ["-svc", "-service"]
    )
    return CandidateLinker(LinkerConfig(window_minutes=15), canonicalizer)


# ---------------------------------------------------------------------------
# the exact edge set
# ---------------------------------------------------------------------------
def test_exact_candidate_edge_set(linker: CandidateLinker):
    """One commit, one deploy-ish decoy, two symptoms — assert every edge."""
    events = [
        commit("cause_commit", 0, ("checkout/app/pool.py",)),
        commit("decoy_commit", 5, ("inventory/views.py",), service="inventory"),
        log_error("effect_log", 40),
        alert("effect_alert", 120),
    ]
    links = linker.link(events)
    actual = {(link.cause_id, link.effect_id, link.heuristics) for link in links}

    assert actual == {
        # the real cause: all three heuristics
        (
            "cause_commit",
            "effect_log",
            ("temporal_proximity", "service_overlap", "change_path_overlap"),
        ),
        # also all three: the commit's `checkout/` path segment maps to the
        # alert's failing service, which is the definition in docs/01
        (
            "cause_commit",
            "effect_alert",
            ("temporal_proximity", "service_overlap", "change_path_overlap"),
        ),
        # the decoy is in the window but touches another service
        ("decoy_commit", "effect_log", ("temporal_proximity",)),
        ("decoy_commit", "effect_alert", ("temporal_proximity",)),
        # symptoms can also precede symptoms
        ("effect_log", "effect_alert", ("temporal_proximity", "service_overlap")),
    }


def test_the_real_cause_outranks_the_decoy(linker: CandidateLinker):
    """The discrimination case — this is what golden incident 0004 tests."""
    events = [
        commit("decoy_commit", 0, ("inventory/views.py",), service="inventory"),
        commit("cause_commit", 5, ("checkout/app/pool.py",)),
        log_error("effect_log", 40),
    ]
    links = linker.link(events)
    assert links[0].cause_id == "cause_commit"
    assert "change_path_overlap" in links[0].heuristics


# ---------------------------------------------------------------------------
# temporal proximity — the off-by-one hunting ground
# ---------------------------------------------------------------------------
def test_effect_exactly_at_the_window_edge_is_included(linker: CandidateLinker):
    events = [commit("c", 0, ()), log_error("e", 15 * 60)]
    assert len(linker.link(events)) == 1


def test_effect_one_second_past_the_window_is_excluded(linker: CandidateLinker):
    events = [commit("c", 0, ()), log_error("e", 15 * 60 + 1)]
    assert linker.link(events) == []


def test_simultaneous_events_are_not_linked(linker: CandidateLinker):
    """Δt = 0 has no direction; inventing one lets an effect explain its cause."""
    events = [commit("c", 30, ()), log_error("e", 30)]
    assert linker.link(events) == []


def test_causes_never_come_after_their_effects(linker: CandidateLinker):
    events = [log_error("e", 10), commit("c", 300, ())]
    assert linker.link(events) == []


def test_window_is_configurable():
    narrow = CandidateLinker(LinkerConfig(window_minutes=1))
    events = [commit("c", 0, ()), log_error("e", 120)]
    assert narrow.link(events) == []

    wide = CandidateLinker(LinkerConfig(window_minutes=30))
    assert len(wide.link(events)) == 1


def test_delta_seconds_is_recorded(linker: CandidateLinker):
    events = [commit("c", 0, ()), log_error("e", 95)]
    assert linker.link(events)[0].delta_seconds == 95


# ---------------------------------------------------------------------------
# service overlap
# ---------------------------------------------------------------------------
def test_service_overlap_fires_on_canonical_names(linker: CandidateLinker):
    """The whole point of canonicalization: `checkout-svc` and `checkout` are
    one service by the time the linker sees them."""
    events = [
        commit("c", 0, (), service="checkout"),
        log_error("e", 30, service="checkout"),
    ]
    assert "service_overlap" in linker.link(events)[0].heuristics


def test_service_overlap_does_not_fire_across_services(linker: CandidateLinker):
    events = [
        commit("c", 0, (), service="checkout"),
        log_error("e", 30, service="inventory"),
    ]
    assert linker.link(events)[0].heuristics == ("temporal_proximity",)


def test_unknown_service_does_not_overlap_with_unknown_service(
    linker: CandidateLinker,
):
    """Two None services are not a match — that would make every unlabelled
    event overlap every other one."""
    events = [commit("c", 0, (), service=None), log_error("e", 30, service=None)]
    assert linker.link(events)[0].heuristics == ("temporal_proximity",)


# ---------------------------------------------------------------------------
# change-path overlap — the strong signal, so guard both directions
# ---------------------------------------------------------------------------
def test_module_stem_matches_the_failure_message(linker: CandidateLinker):
    """`app/pool.py` against "pool exhausted" — the case this heuristic exists
    for, and the one that makes golden incident 0001 work."""
    events = [
        commit("c", 0, ("app/pool.py",), service=None),
        log_error("e", 30, signature="connectionerror: pool exhausted", service=None),
    ]
    assert "change_path_overlap" in linker.link(events)[0].heuristics


def test_path_segment_matches_the_failing_service(linker: CandidateLinker):
    events = [
        commit("c", 0, ("services/checkout/handler.py",), service=None),
        log_error("e", 30, signature="boom", service="checkout"),
    ]
    assert "change_path_overlap" in linker.link(events)[0].heuristics


def test_generic_module_names_do_not_fire(linker: CandidateLinker):
    """`utils.py` changing says nothing about which service broke; matching on
    it would fire the strongest signal on almost every commit."""
    events = [
        commit("c", 0, ("app/utils.py",), service=None),
        log_error("e", 30, signature="utils failed to load", service=None),
    ]
    assert "change_path_overlap" not in linker.link(events)[0].heuristics


def test_short_stems_do_not_fire(linker: CandidateLinker):
    """A 2-3 character stem collides with ordinary words by coincidence."""
    events = [
        commit("c", 0, ("app/db.py",), service=None),
        log_error("e", 30, signature="db connection lost", service=None),
    ]
    assert "change_path_overlap" not in linker.link(events)[0].heuristics


def test_module_matching_is_tokenized_not_substring(linker: CandidateLinker):
    """`pool` must not match `poolside`, or the 0.30-weight signal fires on a
    coincidence."""
    events = [
        commit("c", 0, ("app/pool.py",), service=None),
        log_error(
            "e",
            30,
            summary="poolside cache miss",
            signature="poolside cache miss",
            service=None,
        ),
    ]
    assert "change_path_overlap" not in linker.link(events)[0].heuristics


def test_commit_with_no_files_cannot_overlap(linker: CandidateLinker):
    events = [commit("c", 0, ()), log_error("e", 30)]
    assert "change_path_overlap" not in linker.link(events)[0].heuristics


def test_non_commit_events_with_files_also_overlap(linker: CandidateLinker):
    """A deploy carrying its changed files is as good evidence as a commit."""
    deploy = Event(
        id="d",
        type=EventType.DEPLOY,
        timestamp=at(0),
        source="argo",
        summary="rollout",
        service=None,
        attributes={"files_changed": ("app/pool.py",)},
    )
    events = [deploy, log_error("e", 30, service=None)]
    assert "change_path_overlap" in linker.link(events)[0].heuristics


# ---------------------------------------------------------------------------
# what may be an effect
# ---------------------------------------------------------------------------
def test_deploys_are_never_effects(linker: CandidateLinker):
    """A rollback is caused by the incident, but explaining the outage with the
    fix is useless — and it would crowd out chains that explain something."""
    events = [
        log_error("e", 0),
        Event(
            id="rollback",
            type=EventType.DEPLOY,
            timestamp=at(60),
            source="argo",
            summary="rolled back",
            service="checkout",
        ),
    ]
    assert linker.link(events) == []


def test_alerts_and_logs_are_both_effects(linker: CandidateLinker):
    events = [commit("c", 0, ()), log_error("log", 30), alert("alert", 60)]
    effects = {link.effect_id for link in linker.link(events)}
    assert effects == {"log", "alert"}


# ---------------------------------------------------------------------------
# recorded evidence and ranking
# ---------------------------------------------------------------------------
def test_every_edge_records_which_heuristics_fired(linker: CandidateLinker):
    events = [
        commit("c", 0, ("checkout/app/pool.py",)),
        log_error("e", 30),
    ]
    link = linker.link(events)[0]
    assert link.heuristics == (
        "temporal_proximity",
        "service_overlap",
        "change_path_overlap",
    )
    assert link.score == pytest.approx(1.0)


def test_score_orders_strong_evidence_first(linker: CandidateLinker):
    events = [
        commit("weak", 0, (), service="other"),
        commit("strong", 1, ("checkout/app/pool.py",)),
        log_error("e", 60),
    ]
    links = linker.link(events)
    assert [link.cause_id for link in links] == ["strong", "weak"]
    assert links[0].score > links[1].score


def test_summarize_counts_heuristics(linker: CandidateLinker):
    events = [
        commit("c", 0, ("checkout/app/pool.py",)),
        log_error("e1", 30),
        log_error("e2", 60, summary="upstream gone", signature="timeouterror"),
    ]
    counts = summarize(linker.link(events))
    # 3 edges: c->e1, c->e2, e1->e2
    assert counts["temporal_proximity"] == 3
    # both commit edges overlap — via `pool` for e1, via the `checkout/` path
    # segment for e2. e1->e2 has no files, so it cannot overlap.
    assert counts["change_path_overlap"] == 2


def test_candidate_cap_is_enforced():
    """A backstop against a pathological window producing a quadratic edge set."""
    capped = CandidateLinker(LinkerConfig(window_minutes=60, max_candidates=10))
    events = [commit("c", 0, ())] + [log_error(f"e{i}", 10 + i) for i in range(50)]
    assert len(capped.link(events)) == 10


def test_linking_is_deterministic(linker: CandidateLinker):
    """No LLM in the evidence plane — the same events must always produce the
    same edges, in the same order, or the eval suite means nothing."""
    events = [
        commit("c", 0, ("checkout/app/pool.py",)),
        log_error("e1", 30),
        alert("e2", 60),
    ]
    first = linker.link(events)
    second = linker.link(list(reversed(events)))
    assert first == second


def test_empty_input(linker: CandidateLinker):
    assert linker.link([]) == []


@pytest.mark.parametrize("bad", [{"window_minutes": 0}, {"max_candidates": 0}])
def test_config_rejects_nonsense(bad: dict[str, int]):
    with pytest.raises(ValueError, match="must be > 0"):
        LinkerConfig(**bad)  # type: ignore[arg-type]
