"""Dedup on Valkey: `SET key 1 NX EX <ttl>` — the one atomic primitive this
needs. An alert that flaps five times inside the TTL window produces one Job,
not five; without this, a flapping alert is an API-bill incident of its own
(docs/05).

A thin wrapper, not a cache abstraction: this project has exactly one
Valkey-backed operation, and it doesn't need a general-purpose interface for
that — a class here is only so the redis client can be injected for testing
(a real Valkey via testcontainers, not a mock of the driver — the same
standard `agent/memory.py` holds Neo4j to).
"""

from __future__ import annotations

from dataclasses import dataclass

import redis


@dataclass(frozen=True, slots=True)
class Deduplicator:
    client: redis.Redis
    ttl_seconds: int

    @classmethod
    def from_url(cls, url: str, ttl_seconds: int) -> Deduplicator:
        return cls(
            client=redis.Redis.from_url(url, decode_responses=True),
            ttl_seconds=ttl_seconds,
        )

    def claim(self, key: str) -> bool:
        """True if this call is the first to claim `key` within the TTL —
        the caller should create a Job. False means a duplicate — the caller
        should return success without creating anything, since the first
        call already will (or did)."""
        claimed = self.client.set(key, "1", nx=True, ex=self.ttl_seconds)
        return bool(claimed)
