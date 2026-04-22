"""Autonomous Agents FastAPI Application."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from autonomous_agents.log_config import setup_logging
from autonomous_agents.config import get_settings
from autonomous_agents.routes import health, tasks, webhooks
from autonomous_agents.routes.tasks import set_task_store
from autonomous_agents.routes.webhooks import register_webhook_tasks
from autonomous_agents.scheduler import (
    get_scheduler,
    register_tasks,
    set_chat_history_publisher,
    set_run_store,
)
from autonomous_agents.services.chat_history import NoopChatHistoryPublisher
from autonomous_agents.services.mongo import (
    MongoChatHistoryPublisherAdapter,
    MongoRunStoreAdapter,
    MongoTaskStoreAdapter,
    get_mongo_service,
    reset_mongo_service,
)

logger = setup_logging()


# -----------------------------
# FATAL EXIT
# -----------------------------
def fatal_exit(message: str, exit_code: int = 1) -> None:
    logger.error("FATAL: %s", message)
    raise SystemExit(exit_code)


# -----------------------------
# MONGO CONNECTION LOGIC
# -----------------------------
async def connect_mongo_with_retry(settings):
    """Connect to MongoDB with retry logic. Returns connected client or exits."""
    mongo = get_mongo_service()

    for attempt in range(1, settings.mongodb_connect_max_attempts + 1):
        if await mongo.connect():
            return mongo

        logger.warning(
            "MongoDB connect attempt %d/%d failed; retrying in %.1fs",
            attempt,
            settings.mongodb_connect_max_attempts,
            settings.mongodb_connect_retry_delay_seconds,
        )

        reset_mongo_service()
        mongo = get_mongo_service()

        if attempt < settings.mongodb_connect_max_attempts:
            await asyncio.sleep(settings.mongodb_connect_retry_delay_seconds)

    fatal_exit(
        f"Failed to connect to MongoDB after "
        f"{settings.mongodb_connect_max_attempts} attempt(s). "
        "Check URI, network, and credentials."
    )


# -----------------------------
# LIFESPAN
# -----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting Autonomous Agents service...")

    # ---- Validate config ----
    if not settings.mongodb_uri or not settings.mongodb_database:
        fatal_exit(
            "MONGODB_URI and MONGODB_DATABASE must both be set. "
            f"(URI={'set' if settings.mongodb_uri else 'UNSET'}, "
            f"DB={'set' if settings.mongodb_database else 'UNSET'})"
        )

    # ---- Mongo connect ----
    mongo = await connect_mongo_with_retry(settings)

    # ---- Adapters ----
    task_store = MongoTaskStoreAdapter(mongo)
    run_store = MongoRunStoreAdapter(mongo)

    logger.info(
        "Mongo stores initialized (db=%s, tasks=%s, runs=%s)",
        settings.mongodb_database,
        settings.mongodb_tasks_collection,
        settings.mongodb_collection,
    )

    # ---- Chat history publisher ----
    if settings.chat_history_publish_enabled:
        chat_publisher = MongoChatHistoryPublisherAdapter(mongo)
        logger.info(
            "ChatHistoryPublisher enabled (db=%s)",
            settings.chat_history_database or settings.mongodb_database,
        )
    else:
        chat_publisher = NoopChatHistoryPublisher()
        logger.info("ChatHistoryPublisher disabled")

    # ---- Wire dependencies ----
    set_task_store(task_store)
    set_run_store(run_store)
    set_chat_history_publisher(chat_publisher)

    # ---- Load persisted tasks ----
    runtime_tasks = await task_store.list_all()
    logger.info("Loaded %d task(s) from MongoDB", len(runtime_tasks))

    register_webhook_tasks(runtime_tasks)
    register_tasks(runtime_tasks)

    yield

    # -----------------------------
    # SHUTDOWN
    # -----------------------------
    logger.info("Shutting down Autonomous Agents service...")

    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)

    reset_mongo_service()


# -----------------------------
# APP FACTORY
# -----------------------------
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


# -----------------------------
# DEV ENTRYPOINT
# -----------------------------
if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "autonomous_agents.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )