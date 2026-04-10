FROM python:3.13-slim

WORKDIR /app

# Install uv from official image (avoids pip bootstrap overhead)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy package definition and lock file first for better layer caching
COPY pyproject.toml uv.lock ./
COPY src/ src/

# Install dependencies from lock file (--locked ensures uv.lock is honoured)
RUN UV_HTTP_TIMEOUT=300 uv sync --locked --no-dev

# Copy task config
COPY config.yaml .

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8002

CMD ["uvicorn", "autonomous_agents.main:app", "--host", "0.0.0.0", "--port", "8002"]
