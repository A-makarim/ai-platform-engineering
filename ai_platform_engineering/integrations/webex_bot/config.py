# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Environment-backed configuration for the Webex inbound bridge."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Webex bot configuration.

    Required:
        WEBEX_BOT_TOKEN: Bot access token.
        WEBEX_BOT_PUBLIC_URL: Public base URL for this service.
        AUTONOMOUS_AGENTS_URL: Autonomous Agents service URL.
        MONGODB_URI / MONGODB_DATABASE: Shared MongoDB used for thread mapping.

    Optional:
        WEBEX_WEBHOOK_SECRET: HMAC secret for incoming Webex events.
        WEBHOOK_SECRET: Shared HMAC secret for outbound follow-up requests.
        MONGODB_WEBEX_THREAD_MAP_COLLECTION: Thread-map collection override.
    """

    webex_bot_token: str = Field(...)
    webex_bot_public_url: AnyHttpUrl = Field(...)
    autonomous_agents_url: AnyHttpUrl = Field(...)

    # Mongo (read-only access to the shared thread map collection)
    mongodb_uri: str = Field(...)
    mongodb_database: str = Field(...)
    mongodb_webex_thread_map_collection: str = "webex_thread_map"

    # Optional security
    webex_webhook_secret: str | None = None
    webhook_secret: str | None = None

    # Service knobs
    host: str = "0.0.0.0"  # nosec B104 - intentional for container deployment
    port: int = 8003
    log_level: str = "INFO"

    # Webex API base. Overridable for testing / future tenant migrations.
    webex_api_base: AnyHttpUrl = "https://webexapis.com/v1"  # type: ignore[assignment]

    # HTTP client knobs -- generous defaults so a slow Webex round-trip
    # doesn't drop a legitimate event.
    http_timeout_seconds: float = 15.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()  # type: ignore[call-arg]
