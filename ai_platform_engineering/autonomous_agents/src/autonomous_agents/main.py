"""Autonomous Agents FastAPI Application."""

from contextlib import asynccontextmanager

from autonomous_agents.log_config import setup_logging

logger = setup_logging()

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from autonomous_agents.config import get_settings
from autonomous_agents.routes import health, tasks, webhooks
from autonomous_agents.routes.tasks import set_task_store
from autonomous_agents.routes.webhooks import register_webhook_tasks
from autonomous_agents.scheduler import get_scheduler, register_tasks, set_run_store
from autonomous_agents.services.run_store import MongoRunStore, create_run_store
from autonomous_agents.services.task_loader import load_tasks
from autonomous_agents.services.task_store import (
    MongoTaskStore,
    TaskAlreadyExistsError,
    create_task_store,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting Autonomous Agents service...")

    # Build the run history persistence layer. The factory returns a
    # MongoRunStore when both MONGODB_URI and MONGODB_DATABASE are set;
    # otherwise an InMemoryRunStore so dev environments need no infra.
    run_store = create_run_store(
        mongodb_uri=settings.mongodb_uri,
        mongodb_database=settings.mongodb_database,
        mongodb_collection=settings.mongodb_collection,
        in_memory_maxlen=settings.run_history_maxlen,
    )
    if isinstance(run_store, MongoRunStore):
        await run_store.ensure_indexes()
        logger.info(
            "RunStore: MongoDB (database=%s, collection=%s)",
            settings.mongodb_database,
            settings.mongodb_collection,
        )
    else:
        logger.info(
            "RunStore: in-memory (maxlen=%d) — set MONGODB_URI and MONGODB_DATABASE to persist run history",
            settings.run_history_maxlen,
        )
    set_run_store(run_store)

    # Build the task definition persistence layer. Same factory
    # contract as the run store: Mongo when fully configured, in-memory
    # otherwise. The MongoTaskStore variant survives restarts so
    # UI-driven CRUD changes are not lost.
    task_store = create_task_store(
        mongodb_uri=settings.mongodb_uri,
        mongodb_database=settings.mongodb_database,
        mongodb_collection=settings.mongodb_tasks_collection,
    )
    if isinstance(task_store, MongoTaskStore):
        await task_store.ensure_indexes()
        logger.info(
            "TaskStore: MongoDB (database=%s, collection=%s)",
            settings.mongodb_database,
            settings.mongodb_tasks_collection,
        )
    else:
        logger.info(
            "TaskStore: in-memory — UI-created tasks will be lost on restart; "
            "set MONGODB_URI and MONGODB_DATABASE to persist task definitions"
        )
    set_task_store(task_store)

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
            await task_store.create(task)
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
    runtime_tasks = await task_store.list_all()
    register_webhook_tasks(runtime_tasks)
    register_tasks(runtime_tasks)

    yield

    # Shutdown
    logger.info("Shutting down Autonomous Agents service...")
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)


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
