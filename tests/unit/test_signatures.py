"""The signature table.

docs/06 ranks this the highest-priority test target in the project: wrong
fingerprinting silently ruins every downstream number, and it is a pure
function, so testing it exhaustively is cheap.

Two failure directions, both tested:
  over-collapsing  — unrelated failures merge, manufacturing corroboration
  under-collapsing — one failure splits, manufacturing breadth
"""

from __future__ import annotations

import pytest

from agent.normalize.signatures import (
    MAX_SIGNATURE_LENGTH,
    fingerprint,
    same_failure,
)

# ---------------------------------------------------------------------------
# the table: realistic log lines → expected template
# ---------------------------------------------------------------------------
CASES: list[tuple[str, str]] = [
    # -- the canonical case from docs/01 ----------------------------------
    (
        "ConnectionError: pool exhausted (32/32)",
        "connectionerror: pool exhausted (<num>/<num>)",
    ),
    (
        "ConnectionError: pool exhausted (64/64)",
        "connectionerror: pool exhausted (<num>/<num>)",
    ),
    # -- levels and timestamps stripped from the front --------------------
    (
        "2026-03-12T14:02:11Z ERROR checkout returned 500",
        "checkout returned <num>",
    ),
    (
        "2026-03-12 14:02:11 WARN checkout returned 500",
        "checkout returned <num>",
    ),
    ("[ERROR] disk almost full", "disk almost full"),
    ("error: disk almost full", "disk almost full"),
    # -- identifiers ------------------------------------------------------
    (
        "request 550e8400-e29b-41d4-a716-446655440000 failed",
        "request <uuid> failed",
    ),
    ("segfault at 0x7fff5fbff8c0", "segfault at <hex>"),
    ("trace id a3f9c1e2b4d6f001 dropped", "trace id <hex> dropped"),
    # a long run of pure digits is a number, not hex
    ("alert fired at 1710252131", "alert fired at <num>"),
    # -- network ----------------------------------------------------------
    (
        "connection refused to 10.0.3.17:5432",
        "connection refused to <ip>",
    ),
    (
        "GET https://api.example.com/v2/orders?id=91 timed out",
        "get <url> timed out",
    ),
    ("failed to notify ops-team@example.com", "failed to notify <email>"),
    # -- quoting ----------------------------------------------------------
    (
        'KeyError: "DB_POOL_MAX" not found in settings',
        "keyerror: <str> not found in settings",
    ),
    (
        "cannot parse `SELECT * FROM orders`",
        "cannot parse <str>",
    ),
    # an apostrophe inside a word must not start a quoted region
    ("worker can't reach the primary", "worker can't reach the primary"),
    # -- paths ------------------------------------------------------------
    (
        "IOError: /var/log/checkout/app.log is not writable",
        "ioerror: <path> is not writable",
    ),
    (
        "ImportError: cannot import name from config/database.py",
        "importerror: cannot import name from <path>",
    ),
    (
        "reading ./conf/settings.yaml failed",
        "reading <path> failed",
    ),
    # -- numbers in many shapes -------------------------------------------
    ("retrying in 1.5s", "retrying in <num>s"),
    ("heap at 512MB of 1024MB", "heap at <num>mb of <num>mb"),
    ("upstream returned 503 after 30000ms", "upstream returned <num> after <num>ms"),
    ("processed 4821 records", "processed <num> records"),
    # -- versions collapse, but are not mistaken for paths ----------------
    # "2.3" is consumed as one decimal number and ".1" as another, so this
    # reads v<num>.<num> rather than three parts. What matters is that every
    # v2.x.y collapses to the same template — see the version collapse test.
    ("rolling out checkout v2.3.1", "rolling out checkout v<num>.<num>"),
    # -- whitespace -------------------------------------------------------
    ("  too    many   spaces  ", "too many spaces"),
    ("tab\tseparated\tfields", "tab separated fields"),
    # -- real-world composites --------------------------------------------
    (
        "2026-03-12T14:02:11.482Z ERROR [checkout] "
        'psycopg2.OperationalError: could not connect to server "db-primary" '
        "at 10.0.3.17:5432 (attempt 3/5)",
        "[checkout] psycopg<num>.operationalerror: could not connect to server "
        "<str> at <ip> (attempt <num>/<num>)",
    ),
    (
        "OOMKilled: container checkout-7d9f4b8c6-x2m9p exceeded memory limit of 768Mi",
        "oomkilled: container checkout-<hex>-x<num>m<num>p exceeded memory "
        "limit of <num>mi",
    ),
    (
        "TimeoutError: upstream payment-gateway did not respond within 5000ms",
        "timeouterror: upstream payment-gateway did not respond within <num>ms",
    ),
    (
        "circuit breaker for inventory-svc opened after 12 consecutive failures",
        "circuit breaker for inventory-svc opened after <num> consecutive failures",
    ),
]


@pytest.mark.parametrize(("raw", "expected"), CASES, ids=[c[0][:48] for c in CASES])
def test_fingerprint_table(raw: str, expected: str):
    assert fingerprint(raw) == expected


# ---------------------------------------------------------------------------
# collapsing: same failure, varying detail
# ---------------------------------------------------------------------------
COLLAPSE_GROUPS: list[list[str]] = [
    [
        "ConnectionError: pool exhausted (32/32)",
        "ConnectionError: pool exhausted (64/64)",
        "ConnectionError: pool exhausted (128/128)",
    ],
    [
        "request 550e8400-e29b-41d4-a716-446655440000 failed",
        "request 6ba7b810-9dad-11d1-80b4-00c04fd430c8 failed",
    ],
    [
        "2026-03-12T14:02:11Z ERROR checkout returned 500",
        "2026-03-12T14:09:57Z ERROR checkout returned 503",
    ],
    [
        "connection refused to 10.0.3.17:5432",
        "connection refused to 10.0.3.42:5432",
    ],
    [
        "IOError: /var/log/checkout/app.log is not writable",
        "IOError: /var/log/checkout/app.1.log is not writable",
    ],
    # every patch release of a service must be the same rollout message
    [
        "rolling out checkout v2.3.1",
        "rolling out checkout v2.4.7",
        "rolling out checkout v11.0.2",
    ],
    # the same failure logged at different levels is one failure
    [
        "2026-03-12T14:02:11Z ERROR pool exhausted",
        "2026-03-12T14:02:11Z WARN pool exhausted",
        "[error] pool exhausted",
        "WARNING: pool exhausted",
    ],
]


@pytest.mark.parametrize(
    "group", COLLAPSE_GROUPS, ids=[g[0][:40] for g in COLLAPSE_GROUPS]
)
def test_varying_detail_collapses_to_one_signature(group: list[str]):
    """This is what turns thousands of log lines into a handful of nodes."""
    signatures = {fingerprint(line) for line in group}
    assert len(signatures) == 1, f"expected one signature, got {signatures}"


# ---------------------------------------------------------------------------
# discrimination: genuinely different failures must stay apart
# ---------------------------------------------------------------------------
DISTINCT_PAIRS: list[tuple[str, str]] = [
    (
        "ConnectionError: pool exhausted (32/32)",
        "ConnectionError: connection refused (32/32)",
    ),
    ("TimeoutError: upstream timed out", "TimeoutError: downstream timed out"),
    (
        "KeyError: 'DB_POOL_MAX' not found",
        "KeyError: 'DB_POOL_MAX' not found in cache",
    ),
    (
        "IOError: /var/log/app.log is not writable",
        "IOError: /var/log/app.log is not readable",
    ),
    ("checkout returned 500", "inventory returned 500"),
]


@pytest.mark.parametrize(("left", "right"), DISTINCT_PAIRS)
def test_different_failures_keep_different_signatures(left: str, right: str):
    """Over-collapsing is the more dangerous direction: it merges unrelated
    failures and manufactures corroboration the confidence model then trusts."""
    assert fingerprint(left) != fingerprint(right)


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------
def test_fingerprint_is_deterministic():
    line = "ConnectionError: pool exhausted (32/32)"
    assert fingerprint(line) == fingerprint(line)


def test_fingerprint_is_idempotent():
    """Fingerprinting an already-normalized template must not change it —
    otherwise re-processing an event would move it to a different node."""
    once = fingerprint("ConnectionError: pool exhausted (32/32)")
    assert fingerprint(once) == once


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
def test_blank_messages_have_no_signature(blank: str):
    assert fingerprint(blank) == ""


def test_level_only_message_collapses_to_empty():
    assert fingerprint("ERROR") == ""


def test_long_messages_are_bounded_but_stay_readable():
    """Signatures are graph keys and appear in incident fingerprints, so they
    cannot grow without limit."""
    long_line = "DatabaseError: " + " ".join(f"column_{i}_missing" for i in range(80))
    result = fingerprint(long_line)
    assert len(result) <= MAX_SIGNATURE_LENGTH
    assert result.startswith("databaseerror:")


def test_long_messages_that_differ_get_different_signatures():
    """Truncation alone would collide; the hash suffix is what prevents it."""
    base = "DatabaseError: " + " ".join(f"column_{i}_missing" for i in range(80))
    first = fingerprint(base + " on primary")
    second = fingerprint(base + " on replica")
    assert first != second
    assert len(first) <= MAX_SIGNATURE_LENGTH


def test_same_failure_helper():
    assert same_failure(
        "ConnectionError: pool exhausted (32/32)",
        "ConnectionError: pool exhausted (64/64)",
    )
    assert not same_failure("pool exhausted", "pool refused")
