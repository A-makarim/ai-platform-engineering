"""A2A client — sends tasks to the CAIPE supervisor agent."""

import logging
import uuid
from typing import Any

import httpx

from autonomous_agents.config import get_settings

logger = logging.getLogger("autonomous_agents")


async def invoke_agent(prompt: str, task_id: str, context: dict[str, Any] | None = None) -> str:
    """Send a prompt to the CAIPE supervisor via the A2A protocol.

    Returns the agent's text response, or raises on failure.

    The A2A message format follows the Google A2A spec:
    https://google.github.io/A2A/
    """
    settings = get_settings()
    message_id = str(uuid.uuid4())

    # Augment prompt with any extra context (e.g. webhook payload)
    full_prompt = prompt
    if context:
        import json
        full_prompt = f"{prompt}\n\nContext:\n{json.dumps(context, indent=2)}"

    payload = {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tasks/send",
        "params": {
            "id": message_id,
            "sessionId": f"autonomous-{task_id}",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": full_prompt}],
            },
        },
    }

    logger.info(f"Invoking supervisor at {settings.supervisor_url} for task '{task_id}'")

    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(settings.supervisor_url, json=payload)
        response.raise_for_status()

    result = response.json()

    # Extract text from A2A response parts
    try:
        parts = result["result"]["artifacts"][0]["parts"]
        text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return text.strip()
    except (KeyError, IndexError):
        logger.warning(f"Unexpected A2A response shape: {result}")
        return str(result)
