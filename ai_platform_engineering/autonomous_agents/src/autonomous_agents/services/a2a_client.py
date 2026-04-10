"""A2A client — sends tasks to the CAIPE supervisor agent."""

import json
import logging
import uuid
from typing import Any

import httpx

from autonomous_agents.config import get_settings

logger = logging.getLogger("autonomous_agents")


async def invoke_agent(
    prompt: str,
    task_id: str,
    agent: str | None = None,
    llm_provider: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
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
        full_prompt = f"{prompt}\n\nContext:\n{json.dumps(context, indent=2)}"

    # Build metadata for routing — pass agent name and LLM provider to supervisor
    metadata: dict[str, Any] = {}
    if agent:
        metadata["agent"] = agent
    effective_llm = llm_provider or settings.llm_provider
    if effective_llm:
        metadata["llm_provider"] = effective_llm

    message: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": full_prompt}],
        "messageId": message_id,
        "contextId": f"autonomous-{task_id}",
    }
    if metadata:
        message["metadata"] = metadata

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": message,
            "configuration": {
                "blocking": True,
                "acceptedOutputModes": ["text"],
            },
        },
    }

    logger.info(f"Invoking supervisor at {settings.supervisor_url} for task '{task_id}' (agent={agent!r}, llm_provider={effective_llm!r})")

    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(settings.supervisor_url, json=payload)
        response.raise_for_status()

    result = response.json()

    if "error" in result:
        raise RuntimeError(f"A2A error from supervisor: {result['error']}")

    # Extract text from A2A response using the same 3-step fallback as
    # utils/a2a_common/a2a_remote_agent_connect.py:
    #   1. artifacts[].parts — most agents return results here
    #   2. status.message.parts — used by some agents for final replies
    #   3. history[] last agent message — fallback when neither above is populated
    try:
        task_result = result["result"]

        # 1. Artifacts
        for artifact in task_result.get("artifacts", []):
            texts = [p["text"] for p in artifact.get("parts", []) if p.get("kind") == "text" and p.get("text")]
            if texts:
                return " ".join(texts).strip()

        # 2. Status message parts
        status_parts = task_result.get("status", {}).get("message", {}).get("parts", [])
        texts = [p["text"] for p in status_parts if p.get("kind") == "text" and p.get("text")]
        if texts:
            return " ".join(texts).strip()

        # 3. History — last agent message (skip tool-status emoji lines)
        for message in reversed(task_result.get("history", [])):
            if message.get("role") != "agent":
                continue
            texts = [
                p["text"]
                for p in message.get("parts", [])
                if p.get("kind") == "text"
                and p.get("text")
                and not p["text"].startswith(("🔧", "✅"))
            ]
            if texts:
                return " ".join(texts).strip()

    except (KeyError, TypeError):
        pass

    logger.warning(f"Unexpected A2A response shape: {result}")
    raise RuntimeError(f"Could not extract text from A2A response: {result}")
