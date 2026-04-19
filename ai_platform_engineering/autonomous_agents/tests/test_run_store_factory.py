# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MongoDBService singleton and run collection config."""

from unittest.mock import MagicMock, patch

from autonomous_agents.services.mongo import (
    DEFAULT_COLLECTION_NAME,
    MongoDBService,
    create_mongo_service,
    get_mongo_service,
    reset_mongo_service,
)


def test_create_service_uses_explicit_run_collection():
    client = MagicMock()
    database = MagicMock()
    collection = MagicMock()
    client.__getitem__.return_value = database
    database.__getitem__.return_value = collection

    service = MongoDBService(
        client=client,
        database_name="db",
        run_collection_name="custom_runs",
    )

    assert service.run_collection_name == "custom_runs"
    assert service.get_runs_collection() is collection


def test_create_mongo_service_passes_explicit_run_collection():
    with patch.object(MongoDBService, "_build_client", return_value=MagicMock()):
        service = create_mongo_service(
            mongodb_uri="mongodb://example:27017",
            mongodb_database="db",
            mongodb_collection="custom_runs",
        )

    assert isinstance(service, MongoDBService)
    assert service.run_collection_name == "custom_runs"


def test_get_mongo_service_reuses_singleton():
    reset_mongo_service()
    with patch.object(MongoDBService, "_build_client", return_value=MagicMock()):
        first = get_mongo_service()
        second = get_mongo_service()

    assert first is second
    assert first.run_collection_name == DEFAULT_COLLECTION_NAME
