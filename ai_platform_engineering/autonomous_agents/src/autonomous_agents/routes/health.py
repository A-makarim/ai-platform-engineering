"""Health check endpoint."""

from fastapi import APIRouter
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from autonomous_agents.scheduler import get_scheduler

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    scheduler: AsyncIOScheduler = get_scheduler()
    return {
        "status": "ok",
        "scheduler": scheduler.state,
        "jobs": len(scheduler.get_jobs()),
    }
