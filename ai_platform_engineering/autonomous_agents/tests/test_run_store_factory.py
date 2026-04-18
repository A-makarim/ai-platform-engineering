# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``create_run_store`` factory function."""

from autonomous_agents.services.run_store import (
    InMemoryRunStore,
    MongoRunStore,
    create_run_store,
)


def test_returns_in_memory_when_no_mongo_settings():
    store = create_run_store()
    assert isinstance(store, InMemoryRunStore)


def test_returns_in_memory_when_only_uri_provided():
    """Partial Mongo config should NOT silently engage Mongo — it almost
    always indicates a missing env var, and falling back without telling
    the operator would silently lose run history."""
    store = create_run_store(mongodb_uri="mongodb://example:27017")
    assert isinstance(store, InMemoryRunStore)


def test_returns_in_memory_when_only_database_provided():
    store = create_run_store(mongodb_database="db")
    assert isinstance(store, InMemoryRunStore)


def test_returns_in_memory_when_uri_is_empty_string():
    store = create_run_store(mongodb_uri="", mongodb_database="db")
    assert isinstance(store, InMemoryRunStore)


def test_returns_mongo_when_both_uri_and_database_provided():
    # AsyncIOMotorClient construction is lazy — no network I/O happens
    # until an operation is awaited, so this is safe in a unit test even
    # though no MongoDB is actually running on the bogus URI.
    store = create_run_store(
        mongodb_uri="mongodb://example:27017",
        mongodb_database="db",
    )
    assert isinstance(store, MongoRunStore)


def test_in_memory_maxlen_passed_through():
    store = create_run_store(in_memory_maxlen=42)
    assert isinstance(store, InMemoryRunStore)
    assert store._maxlen == 42


def test_mongo_collection_name_passed_through():
    store = create_run_store(
        mongodb_uri="mongodb://example:27017",
        mongodb_database="db",
        mongodb_collection="custom_runs",
    )
    assert isinstance(store, MongoRunStore)
    assert store._collection.name == "custom_runs"


def test_each_call_returns_a_fresh_instance():
    """The factory does not memoise — singleton management is the
    caller's responsibility (e.g. main.py lifespan), so the factory
    stays trivially testable."""
    s1 = create_run_store()
    s2 = create_run_store()
    assert s1 is not s2
