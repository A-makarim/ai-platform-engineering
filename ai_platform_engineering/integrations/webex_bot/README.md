# Webex Inbound Bridge

Translate in-thread Webex replies into follow-up runs of an autonomous task.

## What It Does

When an autonomous task posts a Webex message, the autonomous-agents service
records the resulting `messageId` in the `webex_thread_map` collection with
the related `task_id` and `run_id`.

This service:

1. Registers a Webex `messages.created` webhook on startup.
2. On every incoming message:
   - Verifies `X-Spark-Signature` when a webhook secret is configured.
   - Skips messages authored by the bot itself.
   - Skips top-level messages with no `parentId`.
   - Looks up the `parentId` in `webex_thread_map`.
   - On a hit, posts a follow-up to autonomous-agents:
     `POST /api/v1/hooks/<task_id>/follow-up`.

The autonomous-agents service then re-runs the original task with the
operator's reply injected as follow-up context.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/healthz` | Liveness check. |
| POST | `/webex/events` | Webex webhook delivery target. |

## Configuration

Required:

| Variable | Description |
| -------- | ----------- |
| `WEBEX_BOT_TOKEN` | Bot access token from <https://developer.webex.com>. |
| `WEBEX_BOT_PUBLIC_URL` | Externally reachable base URL of this service. Webex posts to `<public_url>/webex/events`. Localhost does not work. |
| `AUTONOMOUS_AGENTS_URL` | URL of the autonomous-agents service, for example `http://autonomous-agents:8002`. |
| `MONGODB_URI` | Connection string for the MongoDB used by autonomous-agents. |
| `MONGODB_DATABASE` | Database name, default `caipe`. |

Optional:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `MONGODB_WEBEX_THREAD_MAP_COLLECTION` | `webex_thread_map` | Override only if the autonomous-agents setting of the same name was overridden. |
| `WEBEX_WEBHOOK_SECRET` | none | HMAC-SHA1 secret Webex signs every event with. Strongly recommended in production. |
| `WEBHOOK_SECRET` | none | Service-wide HMAC shared with autonomous-agents for outbound `/follow-up` requests. |
| `WEBEX_API_BASE` | `https://webexapis.com/v1` | Override for testing or future tenant migrations. |
| `HOST` / `PORT` | `0.0.0.0` / `8003` | Bind address. |
| `LOG_LEVEL` | `INFO` | Standard Python logging level. |

Per-task secrets are not supported. The bridge is not part of task creation,
so it cannot know each task's `trigger.secret`. Configure the global
`WEBHOOK_SECRET` on both sides when signed follow-ups are required.

## Running Locally

```bash
# In .env
WEBEX_BOT_TOKEN=...
WEBEX_BOT_PUBLIC_URL=https://abcd.ngrok-free.app
WEBEX_WEBHOOK_SECRET=$(openssl rand -hex 32)
WEBHOOK_SECRET=$(openssl rand -hex 32)

docker compose -f docker-compose.dev.yaml \
  --profile caipe-ui \
  --profile autonomous-agents \
  --profile caipe-supervisor \
  --profile caipe-mongodb \
  --profile webex \
  --profile webex-bot \
  up --build
```

Expose port `8003` to the public internet so Webex can deliver events:

```bash
docker run --rm -it \
  -e NGROK_AUTHTOKEN=... \
  -p 4040:4040 ngrok/ngrok:latest http host.docker.internal:8003
```

Set `WEBEX_BOT_PUBLIC_URL` to the resulting public URL and restart
`webex-bot`.

## Verifying End To End

1. Create a webhook task in the Autonomous tab whose prompt posts a Webex
   message into a known room.
2. Trigger the task with `POST /api/v1/hooks/<task_id>`.
3. Confirm the bot's message arrived in Webex.
4. Reply in-thread to the bot's message.
5. Confirm the bridge logs `Forwarded follow-up: task=... parent_run=...`.
6. Confirm autonomous-agents records a new run with `parent_run_id` set.

## Tests

```bash
cd ai_platform_engineering/integrations/webex_bot
uv venv --python python3.13 --clear .venv
uv sync
uv run pytest
```
