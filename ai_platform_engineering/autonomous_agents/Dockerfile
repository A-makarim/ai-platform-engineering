FROM python:3.13-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy package definition and lock file, then install dependencies reproducibly
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv pip install --system -e .

# Copy task config
COPY config.yaml .

ENV PYTHONUNBUFFERED=1

EXPOSE 8002

CMD ["uvicorn", "autonomous_agents.main:app", "--host", "0.0.0.0", "--port", "8002"]
