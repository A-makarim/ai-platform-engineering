# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :func:`create_task_store`.

Mirrors the assertions in ``test_run_store_factory.py`` so the two
factories follow the same partial-config policy. Anyone debugging a
"why is my data ephemeral?" issue can read either file and find the
same rules.
"""

from datetime import timezone
from unittest.mock import patch

from autonomous_agents.services.task_store import (
    InMemoryTaskStore,
    MongoTaskStore,
    create_task_store,
)


def test_returns_in_memory_store_when_no_mongo_settings():
    store = create_task_store()
    assert isinstance(store, InMemoryTaskStore)


def test_returns_in_memory_store_when_only_uri_provided():
    """Partial config = misconfiguration: better to fall back loudly
    (in-memory, ephemeral) than crash on startup, because Mongo URIs
    are often missing in dev/CI by design."""
    store = create_task_store(mongodb_uri="mongodb://localhost:27017")
    assert isinstance(store, InMemoryTaskStore)


def test_returns_in_memory_store_when_only_database_provided():
    store = create_task_store(mongodb_database="caipe")
    assert isinstance(store, InMemoryTaskStore)


def test_returns_mongo_store_when_both_uri_and_database_provided():
    store = create_task_store(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database="caipe",
    )
    assert isinstance(store, MongoTaskStore)


def test_mongo_client_is_constructed_with_utc_tzinfo():
    """Same rationale as the run-store factory test: we must pin
    ``tz_aware=True`` and ``tzinfo=timezone.utc`` so any future
    datetime fields on tasks (created_at / updated_at) round-trip
    consistently across the API boundary."""
    with patch("motor.motor_asyncio.AsyncIOMotorClient") as mock_client:
        create_task_store(
            mongodb_uri="mongodb://localhost:27017",
            mongodb_database="caipe",
        )

    assert mock_client.called
    _args, kwargs = mock_client.call_args
    assert kwargs.get("tz_aware") is True
    assert kwargs.get("tzinfo") is timezone.utc
