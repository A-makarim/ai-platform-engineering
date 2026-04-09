"""Configuration settings for Autonomous Agents service."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8002
    debug: bool = False

    # LLM (passed through to agents via A2A)
    llm_provider: str = "anthropic-claude"

    # Supervisor A2A endpoint — autonomous agents send tasks here
    supervisor_url: str = "http://localhost:8000"

    # Path to the YAML file that defines scheduled tasks
    task_config_path: str = "config.yaml"

    # Webhook secret for validating incoming webhook payloads (optional)
    webhook_secret: str | None = None

    # CORS
    cors_origins: list[str] = ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
