"""Real Neo4j, un-mocked.

`memory.py` is deliberately not mocked. Mocking a graph driver tests the mock —
and the specific things that break here (MERGE semantics, temporal type
round-tripping, constraint enforcement) are exactly the things a mock asserts
into existence rather than verifies.

How the real Neo4j is provided:
- locally, a testcontainers container per session (`NEO4J_URI` unset);
- in CI, a GitHub Actions service container (`NEO4J_URI` set), so the workflow
  doesn't nest a container inside the runner.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from neo4j import Driver, GraphDatabase
from testcontainers.neo4j import Neo4jContainer

from agent.memory import Neo4jMemory

NEO4J_IMAGE = "neo4j:5-community"
NEO4J_PASSWORD = "testcontainer-local-only"


@pytest.fixture(scope="session")
def neo4j_driver() -> Iterator[Driver]:
    """One connection for the whole session — starting Neo4j costs ~20s, and
    per-test isolation comes from wiping data instead."""
    if uri := os.environ.get("NEO4J_URI"):
        # CI service container: connect directly, no nested container.
        driver = GraphDatabase.driver(
            uri,
            auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
        )
    else:
        container = Neo4jContainer(image=NEO4J_IMAGE, password=NEO4J_PASSWORD)
        container.start()
        driver = GraphDatabase.driver(
            container.get_connection_url(), auth=("neo4j", NEO4J_PASSWORD)
        )
    driver.verify_connectivity()
    yield driver
    driver.close()
    if not os.environ.get("NEO4J_URI"):
        container.stop()


@pytest.fixture
def memory(neo4j_driver: Driver) -> Iterator[Neo4jMemory]:
    """A clean graph per test, with the schema applied."""
    with neo4j_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    store = Neo4jMemory(neo4j_driver)
    store.ensure_schema()
    yield store
    # The driver is session-scoped, so the store must not close it here.
