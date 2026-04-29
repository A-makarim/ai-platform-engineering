# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Webex inbound bridge -- FastAPI service.

End-to-end flow on receipt of a Webex ``messages.created`` event:

    1. ``POST /webex/events`` is called by Webex.
    2. We verify ``X-Spark-Signature`` (HMAC-SHA1 of body) when a
       webhook secret is configured.
    3. We fetch the message body via Webex API (events carry only
       the message id by design).
    4. ``dispatch_message_event`` decides: drop or forward.
    5. If FORWARD, we POST a follow-up to the autonomous-agents
       service which re-fires the original task with the operator's
       reply as additional context.

Webhook registration is idempotent and runs on application startup
so a fresh deploy doesn't require any manual ``curl /webhooks``
ceremony.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from .config import Settings, get_settings
from .dispatcher import (
    Verdict,
    dispatch_message_event,
    forward_followup,
    verify_webex_signature,
)
from .thread_store import WebexThreadStore
from .webex_client import WebexClient


logger = logging.getLogger("webex_bot")


# ---------------------------------------------------------------------------
# App state container
# ---------------------------------------------------------------------------


class AppState:
    """Resources held for the lifetime of the FastAPI app.

    Stashed on ``app.state`` rather than in module-level globals so a
    single test process can spin up multiple isolated apps.
    """

    settings: Settings
    webex: WebexClient
    thread_store: WebexThreadStore
    http: httpx.AsyncClient
    bot_person_id: str
    # ``motor.motor_asyncio.AsyncIOMotorClient`` in production, but
    # we keep the type loose so unit tests can substitute a fake
    # without paying motor's import cost.
    mongo_client: object | None


# ---------------------------------------------------------------------------
# Webhook registration on startup
# ---------------------------------------------------------------------------


async def ensure_webhook_registered(
    webex: WebexClient,
    *,
    target_url: str,
    name: str = "caipe-autonomous-followups",
    secret: str | None = None,
) -> dict[str, Any]:
    """Make sure exactly one ``messages.created`` webhook points at us.

    Idempotent strategy:
        * If a webhook with our ``name`` exists pointing at the same
          ``target_url`` AND its signed/unsigned state matches our
          current ``secret`` argument -- leave it.
        * Otherwise (stale URL OR signed/unsigned mismatch) -- delete
          it and recreate with the current settings. This keeps the
          dev-loop on ngrok painless (rotating the public URL just
          needs a service restart) AND prevents the silent-rejection
          trap where we add a ``WEBEX_WEBHOOK_SECRET`` to ``.env``
          on a second restart but the webhook already exists in
          Webex without a secret -- every event then arrives without
          ``X-Spark-Signature`` and the bot 401s them.
        * If none exist -- create a fresh one.

    We deliberately do NOT scan for "any webhook pointing at this
    target_url" because operators may manage several caipe instances
    against one Webex bot; only webhooks matching ``name`` are ours
    to manage.

    Returns the surviving webhook record.
    """
    existing = await webex.list_webhooks()
    ours = [w for w in existing if w.get("name") == name]

    # Webex's GET /webhooks list response returns ``"secret": ""`` for
    # unsigned webhooks (and omits the field on some tenant flavours);
    # treat both as "no secret configured" for the comparison below.
    desired_signed = bool(secret)

    for wh in ours:
        existing_signed = bool(wh.get("secret"))
        if (
            wh.get("targetUrl") == target_url
            and existing_signed == desired_signed
        ):
            logger.info(
                "Webex webhook %s already points at %s (signed=%s) -- reusing",
                wh.get("id"),
                target_url,
                desired_signed,
            )
            return wh
        # Stale registration (URL changed OR signing posture flipped);
        # nuke and re-create. Logging the precise mismatch reason
        # makes the "I added a secret and now nothing arrives"
        # situation immediately obvious in the startup log.
        reason_bits: list[str] = []
        if wh.get("targetUrl") != target_url:
            reason_bits.append(
                f"url {wh.get('targetUrl')!r} -> {target_url!r}"
            )
        if existing_signed != desired_signed:
            reason_bits.append(
                f"signed {existing_signed} -> {desired_signed}"
            )
        try:
            await webex.delete_webhook(wh["id"])
            logger.info(
                "Deleted stale Webex webhook %s (%s)",
                wh["id"],
                "; ".join(reason_bits) or "no reason captured",
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to delete stale webhook %s: %s", wh["id"], exc)

    created = await webex.create_webhook(
        name=name,
        target_url=target_url,
        resource="messages",
        event="created",
        secret=secret,
    )
    logger.info(
        "Registered Webex webhook %s -> %s (signed=%s)",
        created.get("id"),
        target_url,
        secret is not None,
    )
    return created


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire up dependencies and register the Webex webhook."""
    # Imported lazily so unit tests that import this module don't
    # need motor on PYTHONPATH (the dispatcher and webhook helpers
    # are exercised without ever touching Mongo).
    from motor.motor_asyncio import AsyncIOMotorClient

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    webex = WebexClient(
        token=settings.webex_bot_token,
        base_url=str(settings.webex_api_base),
        timeout=settings.http_timeout_seconds,
    )

    me = await webex.get_me()
    bot_person_id = me.get("id")
    if not bot_person_id:
        # Without our own personId we cannot enforce the loop guard,
        # so fail closed rather than risk an infinite trigger loop.
        await webex.aclose()
        raise RuntimeError(
            "Webex /people/me did not return an id; check WEBEX_BOT_TOKEN"
        )
    logger.info("Webex bot identified as personId=%s", bot_person_id)

    # Mongo (read-only)
    mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
    collection = (
        mongo_client[settings.mongodb_database][
            settings.mongodb_webex_thread_map_collection
        ]
    )
    thread_store = WebexThreadStore(collection)

    http = httpx.AsyncClient(timeout=settings.http_timeout_seconds)

    target_url = f"{str(settings.webex_bot_public_url).rstrip('/')}/webex/events"
    try:
        await ensure_webhook_registered(
            webex,
            target_url=target_url,
            secret=settings.webex_webhook_secret,
        )
    except httpx.HTTPError as exc:
        # Don't crash the bridge if registration fails -- operators
        # may want to register webhooks manually, or the Webex API
        # may be flaky during startup. We log loudly and continue;
        # the /webex/events route still works as long as something
        # else has registered the webhook for us.
        logger.error(
            "Webex webhook registration failed (%s); continuing without "
            "auto-registration. Existing webhooks (if any) will keep "
            "delivering events.",
            exc,
        )

    state = AppState()
    state.settings = settings
    state.webex = webex
    state.thread_store = thread_store
    state.http = http
    state.bot_person_id = bot_person_id
    state.mongo_client = mongo_client
    app.state.bridge = state

    try:
        yield
    finally:
        await webex.aclose()
        await http.aclose()
        mongo_client.close()


# ---------------------------------------------------------------------------
# App + routes
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the FastAPI app. Factory pattern for testability."""

    app = FastAPI(
        title="CAIPE Webex Inbound Bridge",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Intentionally cheap: doesn't touch Webex or Mongo so it
        # can be used as a k8s liveness probe without rate-limiting
        # external services.
        return {"status": "ok"}

    @app.post("/webex/events")
    async def webex_events(
        request: Request,
        x_spark_signature: str | None = Header(None, alias="X-Spark-Signature"),
    ) -> dict[str, Any]:
        """Receive a Webex webhook delivery."""
        bridge: AppState = request.app.state.bridge
        body = await request.body()

        if not verify_webex_signature(
            secret=bridge.settings.webex_webhook_secret,
            body=body,
            signature_header=x_spark_signature,
        ):
            # Don't echo expected vs got; just refuse.
            logger.warning(
                "Rejecting Webex event with bad/missing X-Spark-Signature"
            )
            raise HTTPException(status_code=401, detail="invalid signature")

        try:
            event = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")

        # Webex sometimes sends a confirmation/test ping with no
        # ``data`` -- treat that as a no-op rather than an error so
        # operators see green health checks during setup.
        if not isinstance(event, dict) or "data" not in event:
            return {"status": "ignored", "reason": "no event data"}

        try:
            result = await dispatch_message_event(
                event,
                bot_person_id=bridge.bot_person_id,
                fetch_message=bridge.webex.get_message,
                lookup_thread=bridge.thread_store.lookup,
            )
        except httpx.HTTPError as exc:
            # Failed to fetch the message body. Webex retries on 5xx,
            # so we return 502 and let them try again.
            logger.warning("Webex API error fetching message: %s", exc)
            raise HTTPException(status_code=502, detail="webex api error")

        if result.verdict is not Verdict.FORWARD:
            logger.info(
                "Dropping Webex event: verdict=%s reason=%s",
                result.verdict.value,
                result.reason,
            )
            return {"status": "ignored", "verdict": result.verdict.value}

        assert result.payload is not None  # narrow for the type checker
        try:
            response = await forward_followup(
                result.payload,
                autonomous_agents_url=str(bridge.settings.autonomous_agents_url),
                http_client=bridge.http,
                webhook_secret=bridge.settings.webhook_secret,
            )
        except httpx.HTTPError as exc:
            logger.error(
                "Failed to forward follow-up for task %s: %s",
                result.payload.task_id,
                exc,
            )
            raise HTTPException(
                status_code=502, detail="autonomous-agents unreachable"
            )

        if response.status_code >= 400:
            logger.warning(
                "Follow-up forward returned %s for task=%s parent_run=%s body=%s",
                response.status_code,
                result.payload.task_id,
                result.payload.parent_run_id,
                response.text[:300],
            )
            # Bubble the receiver's status so Webex's delivery dashboard
            # matches what really happened. We deliberately don't 200
            # here -- failed forwards should be retried.
            raise HTTPException(
                status_code=response.status_code,
                detail="follow-up forward failed",
            )

        logger.info(
            "Forwarded follow-up: task=%s parent_run=%s -> %s",
            result.payload.task_id,
            result.payload.parent_run_id,
            response.status_code,
        )
        return {
            "status": "forwarded",
            "task_id": result.payload.task_id,
            "parent_run_id": result.payload.parent_run_id,
        }

    return app


app = create_app()


def main() -> None:
    """Entry point for ``python -m webex_bot``.

    The package is exposed as the flat ``webex_bot`` name in the
    Docker image (``build/Dockerfile.webex-bot`` puts the source
    under ``/app/webex_bot/`` and ``PYTHONPATH=/app``). Tests on a
    monorepo checkout use the same flat name via ``conftest.py``,
    so the import string here is environment-agnostic.
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "webex_bot.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
