# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""MongoDB persistence for Autonomous Agents."""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Any

from autonomous_agents.config import Settings, get_settings
from autonomous_agents.models import TaskDefinition, TaskRun

logger = logging.getLogger("autonomous_agents")

DEFAULT_RUNS_COLLECTION_NAME = "autonomous_runs"
DEFAULT_COLLECTION_NAME = DEFAULT_RUNS_COLLECTION_NAME
DEFAULT_TASKS_COLLECTION_NAME = "autonomous_tasks"


class TaskAlreadyExistsError(Exception):
    """Raised when attempting to create a task whose id already exists."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task '{task_id}' already exists")
        self.task_id = task_id


class TaskNotFoundError(Exception):
    """Raised when a task lookup/update/delete targets an unknown id."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task '{task_id}' not found")
        self.task_id = task_id


class MongoDBService:
    """Shared MongoDB service for Autonomous Agents persistence."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        database_name: str | None = None,
        task_collection_name: str | None = None,
        run_collection_name: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.database_name = database_name or self.settings.mongodb_database
        if not self.database_name:
            raise ValueError("database_name must be a non-empty string")

        self.task_collection_name = (
            task_collection_name
            or self.settings.mongodb_tasks_collection
            or DEFAULT_TASKS_COLLECTION_NAME
        )
        self.run_collection_name = (
            run_collection_name
            or self.settings.mongodb_collection
            or DEFAULT_RUNS_COLLECTION_NAME
        )
        if not self.task_collection_name:
            raise ValueError("task_collection_name must be a non-empty string")
        if not self.run_collection_name:
            raise ValueError("run_collection_name must be a non-empty string")

        self._client = client or self._build_client(self.settings.mongodb_uri)
        self._db = self._client[self.database_name]

    @staticmethod
    def _build_client(mongodb_uri: str) -> Any:
        from motor.motor_asyncio import AsyncIOMotorClient

        return AsyncIOMotorClient(
            mongodb_uri,
            tz_aware=True,
            tzinfo=timezone.utc,
        )

    async def connect(self) -> bool:
        """Verify Mongo connectivity and keep the database handle ready."""
        try:
            await self.ping()
        except Exception as exc:
            logger.error("Failed to connect to MongoDB: %s", exc)
            return False
        logger.info("MongoDB connected (database=%s)", self.database_name)
        return True

    async def ping(self) -> None:
        """Verify connectivity to MongoDB."""
        await self._client.admin.command("ping")

    def disconnect(self) -> None:
        """Close the underlying Mongo client when possible."""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _get_collection(self, collection_name: str) -> Any:
        if not collection_name:
            raise ValueError("collection_name must be a non-empty string")
        return self._db[collection_name]

    def get_runs_collection(self, collection_name: str | None = None) -> Any:
        """Return the collection storing task runs."""
        return self._get_collection(collection_name or self.run_collection_name)

    def get_tasks_collection(self, collection_name: str | None = None) -> Any:
        """Return the collection storing task definitions."""
        return self._get_collection(collection_name or self.task_collection_name)

    async def ensure_run_indexes(self, collection_name: str | None = None) -> None:
        """Create indexes supporting run-history reads."""
        collection = self.get_runs_collection(collection_name)
        await collection.create_index([("task_id", 1), ("started_at", -1)])
        await collection.create_index([("started_at", -1)])

    async def ensure_task_indexes(self, collection_name: str | None = None) -> None:
        """Ensure indexes for task definitions.

        Mongo's automatic ``_id_`` index already covers task-id
        uniqueness and lookup, so there is nothing extra to create yet.
        """
        _ = self.get_tasks_collection(collection_name)
        return None

    async def record_run(
        self,
        run: TaskRun,
        *,
        collection_name: str | None = None,
    ) -> None:
        """Upsert a single task run by ``run_id``."""
        collection = self.get_runs_collection(collection_name)
        doc = run.model_dump()
        doc["_id"] = run.run_id
        await collection.replace_one({"_id": run.run_id}, doc, upsert=True)

    async def list_runs(
        self,
        *,
        limit: int = 500,
        collection_name: str | None = None,
    ) -> list[TaskRun]:
        """List runs newest first across all tasks."""
        if limit <= 0:
            return []
        collection = self.get_runs_collection(collection_name)
        cursor = collection.find({}, sort=[("started_at", -1)]).limit(limit)
        return [self._doc_to_run(doc) async for doc in cursor]

    async def list_runs_by_task(
        self,
        task_id: str,
        *,
        limit: int = 100,
        collection_name: str | None = None,
    ) -> list[TaskRun]:
        """List runs newest first for a single task."""
        if limit <= 0:
            return []
        collection = self.get_runs_collection(collection_name)
        cursor = collection.find(
            {"task_id": task_id},
            sort=[("started_at", -1)],
        ).limit(limit)
        return [self._doc_to_run(doc) async for doc in cursor]

    async def list_tasks(
        self,
        *,
        collection_name: str | None = None,
    ) -> list[TaskDefinition]:
        """List all tasks sorted by id."""
        collection = self.get_tasks_collection(collection_name)
        cursor = collection.find({}, sort=[("_id", 1)])
        return [self._doc_to_task(doc) async for doc in cursor]

    async def get_task(
        self,
        task_id: str,
        *,
        collection_name: str | None = None,
    ) -> TaskDefinition | None:
        """Return a task by id, or ``None`` if missing."""
        collection = self.get_tasks_collection(collection_name)
        doc = await collection.find_one({"_id": task_id})
        return self._doc_to_task(doc) if doc else None

    async def create_task(
        self,
        task: TaskDefinition,
        *,
        collection_name: str | None = None,
    ) -> TaskDefinition:
        """Insert a new task definition."""
        collection = self.get_tasks_collection(collection_name)
        doc = self._task_to_doc(task)
        try:
            await collection.insert_one(doc)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "DuplicateKeyError":
                raise TaskAlreadyExistsError(task.id) from exc
            raise
        return task

    async def update_task(
        self,
        task_id: str,
        task: TaskDefinition,
        *,
        collection_name: str | None = None,
    ) -> TaskDefinition:
        """Replace an existing task definition."""
        if task.id != task_id:
            raise ValueError(
                f"path task_id '{task_id}' does not match body id '{task.id}'"
            )
        collection = self.get_tasks_collection(collection_name)
        doc = self._task_to_doc(task)
        result = await collection.replace_one({"_id": task_id}, doc, upsert=False)
        if result.matched_count == 0:
            raise TaskNotFoundError(task_id)
        return task

    async def delete_task(
        self,
        task_id: str,
        *,
        collection_name: str | None = None,
    ) -> None:
        """Delete a task definition by id."""
        collection = self.get_tasks_collection(collection_name)
        result = await collection.delete_one({"_id": task_id})
        if result.deleted_count == 0:
            raise TaskNotFoundError(task_id)

    @staticmethod
    def _doc_to_run(doc: dict[str, Any]) -> TaskRun:
        payload = dict(doc)
        payload.pop("_id", None)
        return TaskRun.model_validate(payload)

    @staticmethod
    def _task_to_doc(task: TaskDefinition) -> dict[str, Any]:
        doc = task.model_dump(mode="json")
        doc["_id"] = task.id
        return doc

    @staticmethod
    def _doc_to_task(doc: dict[str, Any]) -> TaskDefinition:
        payload = dict(doc)
        payload.pop("_id", None)
        return TaskDefinition.model_validate(payload)


_mongo_service: MongoDBService | None = None


def create_mongo_service(
    mongodb_uri: str | None = None,
    mongodb_database: str | None = None,
    mongodb_tasks_collection: str | None = None,
    mongodb_collection: str | None = None,
) -> MongoDBService:
    """Create a Mongo service from explicit args or settings defaults."""
    settings = get_settings()
    if any(
        value is not None
        for value in (
            mongodb_uri,
            mongodb_database,
            mongodb_tasks_collection,
            mongodb_collection,
        )
    ):
        database_name = mongodb_database or settings.mongodb_database
        client = MongoDBService._build_client(mongodb_uri or settings.mongodb_uri)
        return MongoDBService(
            client=client,
            database_name=database_name,
            task_collection_name=(
                mongodb_tasks_collection or settings.mongodb_tasks_collection
            ),
            run_collection_name=(mongodb_collection or settings.mongodb_collection),
        )
    return MongoDBService()


def get_mongo_service() -> MongoDBService:
    """Return the process-wide Mongo service singleton."""
    global _mongo_service
    if _mongo_service is None:
        _mongo_service = create_mongo_service()
    return _mongo_service


def set_mongo_service(service: MongoDBService) -> None:
    """Override the process-wide Mongo service singleton."""
    global _mongo_service
    _mongo_service = service


def reset_mongo_service() -> None:
    """Drop the cached Mongo service singleton."""
    global _mongo_service
    if _mongo_service is not None:
        _mongo_service.disconnect()
        _mongo_service = None
