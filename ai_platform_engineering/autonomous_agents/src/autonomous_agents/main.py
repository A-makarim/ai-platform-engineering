"""Autonomous Agents FastAPI Application."""

from contextlib import asynccontextmanager

from autonomous_agents.log_config import setup_logging

logger = setup_logging()

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from autonomous_agents.config import get_settings
from autonomous_agents.routes import health, tasks, webhooks
from autonomous_agents.routes.tasks import set_registered_tasks
from autonomous_agents.routes.webhooks import register_webhook_tasks
from autonomous_agents.scheduler import get_scheduler, register_tasks
from autonomous_agents.services.task_loader import load_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting Autonomous Agents service...")

    # Load task definitions from YAML
    loaded_tasks = load_tasks(settings.task_config_path)

    # Share task list with route handlers
    set_registered_tasks(loaded_tasks)
    register_webhook_tasks(loaded_tasks)

    # Start the scheduler (registers cron + interval tasks)
    register_tasks(loaded_tasks)

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
