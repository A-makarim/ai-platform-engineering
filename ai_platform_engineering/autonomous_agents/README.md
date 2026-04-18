# Autonomous Agents

A standalone FastAPI service that schedules and triggers AI agents to run in the background — without a human in the loop.

Part of the [CAIPE (Community AI Platform Engineering)](https://cnoe-io.github.io/ai-platform-engineering/) project, developed in collaboration with **Cisco Outshift** and **UCL**.

---

## Overview

While the main CAIPE supervisor handles on-demand, chat-driven tasks, Autonomous Agents handles **scheduled and event-driven** tasks:

- Run an agent on a **cron schedule** (e.g. daily security scan at 09:00 UTC)
- Run an agent at a fixed **interval** (e.g. health check every 30 minutes)
- Run an agent when an external system fires a **webhook** (e.g. GitHub PR opened)

All tasks are defined in a single `config.yaml` file. No code changes needed to add or modify tasks.

---

## Architecture

```
config.yaml (task definitions)
        |
        v
  +--------------------------+
  |  Autonomous Agents       |  FastAPI :8002
  |  +------------+          |
  |  | Scheduler  | APScheduler (cron / interval)
  |  +-----+------+          |
  |        |  webhook POST   |
  |  +-----v------+          |
  |  | Task Runner|          |
  |  +-----+------+          |
  +---------|-----------------+
            |  A2A protocol
            v
  +--------------------------+
  |  CAIPE Supervisor        |  :8000
  |  (LangGraph ReAct agent) |
  +--------------------------+
            |
            v
  Sub-agents: GitHub, ArgoCD, Jira, PagerDuty ...
```

Tasks are loaded at startup from `config.yaml`. Each task is sent to the CAIPE supervisor via the [A2A protocol](https://google.github.io/A2A/) when its trigger fires. The supervisor routes the task to the appropriate sub-agent.

---

## Project Structure

```
autonomous_agents/
  src/autonomous_agents/
    main.py               # FastAPI app entrypoint
    config.py             # Settings (env vars)
    models.py             # Pydantic models: TaskDefinition, triggers, run records
    scheduler.py          # APScheduler - registers and fires cron/interval tasks
    log_config.py         # Logging with task_id context
    routes/
      health.py           # GET /health
      tasks.py            # GET /api/v1/tasks, /runs, POST /tasks/{id}/run
      webhooks.py         # POST /api/v1/hooks/{task_id}
    services/
      task_loader.py      # Parses config.yaml into TaskDefinition objects
      a2a_client.py       # Sends prompts to CAIPE supervisor via A2A
  config.yaml             # Task definitions
  pyproject.toml
  Dockerfile
```

---

## Trigger Types

### Cron
Runs on a standard cron schedule (UTC).

```yaml
trigger:
  type: cron
  schedule: "0 9 * * 1-5"   # 09:00 UTC, Monday-Friday
```

### Interval
Runs repeatedly at a fixed time interval.

```yaml
trigger:
  type: interval
  minutes: 30              # also supports: seconds, hours
```

### Webhook
Runs when an external system POSTs to `/api/v1/hooks/{task_id}`.

```yaml
trigger:
  type: webhook
  path: "/hooks/pr-review"
  secret: "optional-hmac-secret"   # validates X-Hub-Signature-256 header
```

---

## Configuration

### config.yaml

Full task definition schema:

```yaml
tasks:
  - id: "my-task"                    # unique identifier (used in API + webhook URL)
    name: "My Task"                  # human-readable label
    description: "Optional"
    agent: "github"                  # CAIPE agent to invoke (must be enabled in supervisor)
    prompt: |                        # prompt sent to the agent
      Check all open PRs and flag any that have been open for more than 7 days.
    trigger:
      type: cron
      schedule: "0 9 * * *"
    llm_provider: "aws-bedrock"      # optional: overrides global LLM_PROVIDER
    enabled: true
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SUPERVISOR_URL` | `http://localhost:8000` | CAIPE supervisor A2A endpoint |
| `TASK_CONFIG_PATH` | `config.yaml` | Path to task definitions file |
| `LLM_PROVIDER` | `anthropic-claude` | Default LLM provider |
| `HOST` | `0.0.0.0` | Server bind host |
| `PORT` | `8002` | Server port |
| `WEBHOOK_SECRET` | `None` | Global HMAC secret for webhook validation |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MONGODB_URI` | `None` | Optional. Enables MongoDB-backed run history. See *Run History Persistence*. |
| `MONGODB_DATABASE` | `None` | Optional. MongoDB database name. Required together with `MONGODB_URI`. |
| `MONGODB_COLLECTION` | `autonomous_runs` | MongoDB collection name for run history. |
| `RUN_HISTORY_MAXLEN` | `500` | Max runs retained by the in-memory store when MongoDB is not configured. |

---

## Run History Persistence

The service records one entry per task run (a `TaskRun`) and exposes
them via `GET /api/v1/runs` and `GET /api/v1/tasks/{id}/runs`.

Two backends are supported and selected automatically by environment
variables. Both implement the same `RunStore` protocol so the
scheduler and HTTP routes are agnostic to which one is active:

| Mode | Activated by | Trade-offs |
|---|---|---|
| **In-memory (default)** | Neither `MONGODB_URI` nor `MONGODB_DATABASE` set | Zero infra, instant startup. **Lost on restart**. Bounded by `RUN_HISTORY_MAXLEN` (default 500), oldest evicted FIFO. Suitable for development and demos. |
| **MongoDB** | **Both** `MONGODB_URI` *and* `MONGODB_DATABASE` set | Persistent across restarts, queryable from external tools, no eviction. Required for production and for the upcoming UI integration (the UI reads run history from this store). |

Partial Mongo configuration (only `MONGODB_URI` or only
`MONGODB_DATABASE`) is treated as **not configured** and falls back
to in-memory — silently engaging Mongo on half-config would mask
typical env-var typos and write history to the wrong place.

The MongoDB schema is one document per run, mirroring the `TaskRun`
model. Three indexes are created automatically at startup:

- Unique on `run_id` — guards against duplicate inserts on retry.
- Compound `(task_id ASC, started_at DESC)` — backs the
  list-by-task query (`GET /tasks/{id}/runs`) without a collection
  scan.
- `started_at DESC` — backs the global list-all query
  (`GET /runs`). The compound index above leads on `task_id`, so
  Mongo will not use it for an unfiltered sort across tasks.

The startup log line tells you which backend is active:

```
RunStore: MongoDB (database=autonomous_agents, collection=autonomous_runs)
RunStore: in-memory (maxlen=500) — set MONGODB_URI and MONGODB_DATABASE to persist run history
```

---

## Getting Started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- A running CAIPE supervisor (see root [README](../../../../README.md))

### Install and Run Locally

```bash
cd ai_platform_engineering/autonomous_agents

# Install dependencies
uv venv --python python3.13 .venv
uv pip install -e .

# Configure
cp ../../.env .env
echo "SUPERVISOR_URL=http://localhost:8000" >> .env

# Edit config.yaml - set enabled: true on at least one task

# Run
uv run uvicorn autonomous_agents.main:app --port 8002 --reload
```

### Run with Docker

```bash
docker build -t autonomous-agents .
docker run -p 8002:8002 \
  -e SUPERVISOR_URL=http://host.docker.internal:8000 \
  -e LLM_PROVIDER=anthropic-claude \
  autonomous-agents
```

### API

Once running, the interactive API docs are at `http://localhost:8002/docs`.

| Endpoint | Description |
|---|---|
| `GET /health` | Service health + scheduler status |
| `GET /api/v1/tasks` | List all tasks and next scheduled run |
| `GET /api/v1/tasks/{id}/runs` | Run history for a specific task |
| `POST /api/v1/tasks/{id}/run` | Manually trigger a task immediately |
| `GET /api/v1/runs` | Full run history across all tasks |
| `POST /api/v1/hooks/{task_id}` | Webhook endpoint for a task |

---

## Adding a New Task

1. Open `config.yaml`
2. Add a new entry under `tasks:`
3. Set `enabled: true`
4. Restart the service (or it will pick up changes on next restart)

No code changes required.

---

## Supported LLM Providers

Per task via the `llm_provider` field, or globally via `LLM_PROVIDER` env var:

| Value | Provider |
|---|---|
| `anthropic-claude` | Anthropic Claude API |
| `aws-bedrock` | AWS Bedrock |
| `openai` | OpenAI API |
| `azure-openai` | Azure OpenAI |

---

## Contributing

Follow the project-wide contribution guidelines in [AGENTS.md](../../../../AGENTS.md) and [CLAUDE.md](../../../../CLAUDE.md):

- Branch naming: `prebuild/feat/autonomous-agents-<description>`
- Commits: conventional commits + DCO sign-off (`git commit -s`)
- Lint before committing: `uv run ruff check src/`
