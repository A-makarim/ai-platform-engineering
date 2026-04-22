"""Load task definitions from the YAML config file."""

import logging
from pathlib import Path
from typing import Any

import yaml

from autonomous_agents.models import TaskDefinition

logger = logging.getLogger("autonomous_agents")


def load_tasks(config_path: str) -> list[TaskDefinition]:
    """Read config.yaml and return validated TaskDefinition objects.

    Silently skips disabled tasks and logs any validation errors so one bad
    task definition does not prevent the rest from loading.
    """
    path = Path(config_path)
    if not path.exists():
        logger.warning(f"Task config not found at {config_path} — no tasks loaded")
        return []

    with path.open() as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    raw_tasks: list[dict] = raw.get("tasks", [])
    tasks: list[TaskDefinition] = []

    for raw_task in raw_tasks:
        try:
            task = TaskDefinition.model_validate(raw_task)
            if not task.enabled:
                logger.info(f"Task '{task.id}' is disabled — skipping")
                continue
            tasks.append(task)
            logger.info(f"Loaded task '{task.id}' ({task.trigger.type})")
        except Exception as e:
            task_id = raw_task.get("id", "<unknown>")
            logger.error(f"Failed to parse task '{task_id}': {e}")

    logger.info(f"Loaded {len(tasks)} task(s) from {config_path}")
    return tasks
