"""Real Neo4j via testcontainers.

`memory.py` is deliberately not mocked. Mocking a graph driver tests the mock —
and the specific things that break here (MERGE semantics, temporal type
round-tripping, constraint enforcement) are exactly the things a mock asserts
into existence rather than verifies.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from neo4j import Driver, GraphDatabase
from testcontainers.neo4j import Neo4jContainer

from agent.memory import Neo4jMemory

NEO4J_IMAGE = "neo4j:5-community"
NEO4J_PASSWORD = "testcontainer-local-only"


@pytest.fixture(scope="session")
def neo4j_driver() -> Iterator[Driver]:
    """One container for the whole session — starting Neo4j costs ~20s, and
    per-test isolation comes from wiping data instead."""
    container = Neo4jContainer(image=NEO4J_IMAGE, password=NEO4J_PASSWORD)
    with container:
        driver = GraphDatabase.driver(
            container.get_connection_url(), auth=("neo4j", NEO4J_PASSWORD)
        )
        driver.verify_connectivity()
        yield driver
        driver.close()


@pytest.fixture
def memory(neo4j_driver: Driver) -> Iterator[Neo4jMemory]:
    """A clean graph per test, with the schema applied."""
    with neo4j_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    store = Neo4jMemory(neo4j_driver)
    store.ensure_schema()
    yield store
    # The driver is session-scoped, so the store must not close it here.
