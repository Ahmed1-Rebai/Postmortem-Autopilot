"""`Deduplicator` against a real Valkey, via testcontainers.

Not mocked, for the same reason `agent/memory.py` isn't: the one thing this
class does is lean on `SET NX EX`'s atomicity, and a mock of the redis client
would only ever assert that behavior into existence, never verify it. Same
image as `docker-compose.yml`'s own choice (`valkey/valkey:8-alpine`), not a
generic `redis:latest` — Valkey and Redis's OSS `SET NX EX` semantics agree,
but there's no reason to test against a different server than the one this
project actually runs.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from testcontainers.redis import RedisContainer

from receiver.dedup import Deduplicator

VALKEY_IMAGE = "valkey/valkey:8-alpine"


@pytest.fixture(scope="session")
def valkey_container() -> Iterator[RedisContainer]:
    container = RedisContainer(image=VALKEY_IMAGE)
    with container:
        yield container


@pytest.fixture
def dedup(valkey_container: RedisContainer) -> Deduplicator:
    client = valkey_container.get_client(decode_responses=True)
    client.flushall()
    return Deduplicator(client=client, ttl_seconds=1)


def test_first_claim_succeeds(dedup: Deduplicator):
    assert dedup.claim("dedup:group1:2026-03-12T14:00:00+00:00") is True


def test_repeat_claim_within_ttl_fails(dedup: Deduplicator):
    key = "dedup:group1:2026-03-12T14:00:00+00:00"
    assert dedup.claim(key) is True
    assert dedup.claim(key) is False
    assert dedup.claim(key) is False


def test_distinct_keys_each_get_their_own_claim(dedup: Deduplicator):
    assert dedup.claim("dedup:group1:t1") is True
    assert dedup.claim("dedup:group2:t1") is True
    assert dedup.claim("dedup:group1:t2") is True


def test_claim_expires_after_ttl(dedup: Deduplicator):
    """A flapping alert re-fires after the TTL lapses — this is the mechanism
    that turns a five-times-flapping alert into one Job, not zero forever."""
    key = "dedup:group1:t1"
    assert dedup.claim(key) is True
    time.sleep(1.2)  # ttl_seconds=1 on the fixture
    assert dedup.claim(key) is True


def test_from_url_constructs_a_working_client(valkey_container: RedisContainer):
    host = valkey_container.get_container_host_ip()
    port = valkey_container.get_exposed_port(valkey_container.port)
    dedup = Deduplicator.from_url(f"redis://{host}:{port}/0", ttl_seconds=10)
    dedup.client.flushall()
    assert dedup.claim("dedup:from-url-test") is True
    assert dedup.claim("dedup:from-url-test") is False
