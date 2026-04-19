# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MongoDBService task collection configuration."""

from unittest.mock import MagicMock, patch

from autonomous_agents.services.mongo import (
    DEFAULT_TASKS_COLLECTION_NAME,
    MongoDBService,
    create_mongo_service,
)


def test_create_service_uses_explicit_task_collection():
    client = MagicMock()
    database = MagicMock()
    collection = MagicMock()
    client.__getitem__.return_value = database
    database.__getitem__.return_value = collection

    service = MongoDBService(
        client=client,
        database_name="caipe",
        task_collection_name="custom_tasks",
    )

    assert service.task_collection_name == "custom_tasks"
    assert service.get_tasks_collection() is collection


def test_create_mongo_service_passes_explicit_task_collection():
    with patch.object(MongoDBService, "_build_client", return_value=MagicMock()):
        service = create_mongo_service(
            mongodb_uri="mongodb://localhost:27017",
            mongodb_database="caipe",
            mongodb_tasks_collection="custom_tasks",
        )

    assert isinstance(service, MongoDBService)
    assert service.task_collection_name == "custom_tasks"


def test_default_task_collection_name_is_preserved():
    with patch.object(MongoDBService, "_build_client", return_value=MagicMock()):
        service = create_mongo_service(
            mongodb_uri="mongodb://localhost:27017",
            mongodb_database="caipe",
        )

    assert service.task_collection_name == DEFAULT_TASKS_COLLECTION_NAME
