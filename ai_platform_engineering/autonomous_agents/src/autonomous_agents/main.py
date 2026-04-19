"""Autonomous Agents FastAPI Application."""

from contextlib import asynccontextmanager

from autonomous_agents.log_config import setup_logging

logger = setup_logging()

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from autonomous_agents.config import get_settings
from autonomous_agents.routes import health, tasks, webhooks
from autonomous_agents.routes.tasks import set_persistence_service as set_task_service
from autonomous_agents.routes.webhooks import register_webhook_tasks
from autonomous_agents.scheduler import (
    get_scheduler,
    register_tasks,
    set_chat_history_publisher,
    set_persistence_service as set_scheduler_service,
)
from autonomous_agents.services.chat_history import (
    MongoChatHistoryPublisher,
    create_chat_history_publisher,
)
from autonomous_agents.services.mongo import (
    TaskAlreadyExistsError,
    create_mongo_service,
    reset_mongo_service,
    set_mongo_service,
)
from autonomous_agents.services.task_loader import load_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting Autonomous Agents service...")

    reset_mongo_service()
    mongo_service = create_mongo_service(
        mongodb_database=settings.mongodb_database,
        mongodb_tasks_collection=settings.mongodb_tasks_collection,
        mongodb_collection=settings.mongodb_collection,
    )
    # Seed the process-wide singleton so any legacy singleton access
    # during startup shares the same configured service instance.
    set_mongo_service(mongo_service)
    await mongo_service.ping()
    logger.info("MongoDB connected (database=%s)", settings.mongodb_database)

    await mongo_service.ensure_run_indexes()
    logger.info(
        "Run history: MongoDB (database=%s, collection=%s)",
        settings.mongodb_database,
        settings.mongodb_collection,
    )
    set_scheduler_service(mongo_service)

    await mongo_service.ensure_task_indexes()
    logger.info(
        "Task catalog: MongoDB (database=%s, collection=%s)",
        settings.mongodb_database,
        settings.mongodb_tasks_collection,
    )
    set_task_service(mongo_service)

    # IMP-13: build the chat-history publisher. No-op when the feature
    # is disabled (the default) so the chat database stays untouched.
    # When enabled it shares the run-store's MONGODB_URI but can target
    # a different logical database via CHAT_HISTORY_DATABASE.
    chat_publisher = create_chat_history_publisher(
        enabled=settings.chat_history_publish_enabled,
        mongodb_uri=settings.mongodb_uri,
        chat_database=settings.chat_history_database,
        fallback_database=settings.mongodb_database,
        owner_email=settings.chat_history_owner_email,
        conversations_collection=settings.chat_history_conversations_collection,
        messages_collection=settings.chat_history_messages_collection,
    )
    if isinstance(chat_publisher, MongoChatHistoryPublisher):
        # Best-effort index creation: a transient chat-DB outage or a
        # missing ``createIndex`` permission must NOT take down the
        # autonomous service (chat-history publishing is observability,
        # not source-of-truth -- same contract as ``_publish_safely``
        # in the scheduler). PR #10 Codex P1 review.
        try:
            await chat_publisher.ensure_indexes()
        except Exception as exc:
            logger.error(
                "ChatHistoryPublisher: ensure_indexes() failed (%s) -- "
                "continuing without dedicated chat-history indexes; "
                "queries will still work but may be slower until the "
                "operator creates the indexes manually.",
                exc,
            )
        logger.info(
            "ChatHistoryPublisher: MongoDB (database=%s, owner=%s)",
            settings.chat_history_database or settings.mongodb_database,
            settings.chat_history_owner_email,
        )
    else:
        logger.info(
            "ChatHistoryPublisher: disabled (set CHAT_HISTORY_PUBLISH_ENABLED=true "
            "with MONGODB_URI/MONGODB_DATABASE to surface autonomous runs in the chat sidebar)"
        )
    set_chat_history_publisher(chat_publisher)

    # Load task definitions from YAML and seed the store.
    # ``create()`` raises TaskAlreadyExistsError for ids already present
    # in the store -- we treat that as "operator has already taken
    # ownership of this id via the UI" and skip silently. This makes
    # the YAML file act as a *default* set of tasks for fresh installs
    # while leaving live MongoDB-backed deployments alone.
    yaml_tasks = load_tasks(settings.task_config_path)
    seeded = 0
    for task in yaml_tasks:
        try:
            await mongo_service.create_task(task)
            seeded += 1
        except TaskAlreadyExistsError:
            continue
    logger.info(
        "Seeded %d task(s) from %s (skipped %d already-present)",
        seeded,
        settings.task_config_path,
        len(yaml_tasks) - seeded,
    )

    # Read the canonical task list back from the store (which now
    # includes both YAML defaults and any persisted CRUD edits).
    runtime_tasks = await mongo_service.list_tasks()
    register_webhook_tasks(runtime_tasks)
    register_tasks(runtime_tasks)

    yield

    # Shutdown
    logger.info("Shutting down Autonomous Agents service...")
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    reset_mongo_service()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Autonomous Agents Service",
        description="Schedule and trigger AI agents to run in the background autonomously",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(tasks.router, prefix="/api/v1")
    app.include_router(webhooks.router, prefix="/api/v1")

    @app.get("/")
    async def root():
        return {
            "service": "autonomous-agents",
            "version": "0.1.0",
            "docs": "/docs",
        }

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "autonomous_agents.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
