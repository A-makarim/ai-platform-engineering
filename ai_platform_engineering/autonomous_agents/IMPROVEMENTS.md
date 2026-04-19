# Autonomous Agents — Improvement Tracker

> Living checklist of improvements for the `autonomous_agents` service.
> Cut completed items by deleting them (or moving them to a `## Done` section).
> Each item has an **ID**, **status**, **why it matters**, and a **suggested approach**
> so we can pick any one up without re-deriving the context.

---

## North Star

**End goal**: integrate `autonomous_agents` into the CAIPE UI so end-users can
create, edit, run and monitor autonomous (scheduled / triggered) tasks
from the same interface they use for chat, Skills, and Task Builder —
without ever editing `config.yaml` by hand.

Every improvement below is either a prerequisite for that, a hardening item
needed before this is production-deployable, or a quality-of-life fix.

---

## Status legend

| Marker      | Meaning                                                |
|-------------|--------------------------------------------------------|
| `TODO`      | Not started.                                           |
| `IN PROGRESS` | Actively being worked on.                            |
| `BLOCKED`   | Waiting on an external decision / dependency.          |
| `DONE`      | Completed — delete the item or move under `## Done`.   |

---

## Phase 1 — Hardening (do these before anything UI-facing)

These make the service safe to leave running and trustworthy enough to build
the UI on top of. None of them change the public API shape.

---

_(IMP-03 — completed; see Done section.)_

---

_(IMP-04 — completed; see Done section.)_

---

_(IMP-05 — completed; see Done section.)_

---

### IMP-06 — Validate `metadata.agent` hint end-to-end vs the live supervisor
- **Status**: TODO
- **Why**: Open question #1 in `NOTES.md`. Today `a2a_client.py` sends
  `metadata={"agent": task.agent}` optimistically. If the supervisor's LLM
  router ignores it, the README is misleading and we're paying for an extra
  routing LLM call. We need to know before building UI on top.
- **Approach**:
  1. Manual: send the same prompt with and without `metadata.agent`,
     observe routing in supervisor logs.
  2. Codify the result: either drop the hint and accept LLM routing, or
     propose a small supervisor change for hint-based fast-path routing
     (separate PR upstream).
  3. Update `README.md` to match reality.
- **Touches**: `services/a2a_client.py` (maybe), `README.md`, `NOTES.md` (close Q1).

---

_(IMP-07 — completed; see Done section.)_

---

### IMP-08 — Distributed tracing (W3C `traceparent` propagation)
- **Status**: TODO
- **Why**: Today an autonomous run is invisible in supervisor traces. Hard to
  debug "why did the daily security scan fail at 09:00 UTC?".
- **Approach**:
  1. Bring in `opentelemetry-api` + `opentelemetry-instrumentation-httpx` +
     `opentelemetry-instrumentation-fastapi`.
  2. In `_execute_task`, start a span `autonomous.run` with attributes
     `task.id`, `task.agent`, `trigger.type`.
  3. The httpx instrumentation will inject `traceparent` automatically.
- **Touches**: `pyproject.toml`, `main.py`, `scheduler.py`,
  `services/a2a_client.py`.

---

_(IMP-09 — completed; see Done section.)_

---

## Phase 2 — Production readiness + UI integration (the END GOAL)

Phase 2 only makes sense after Phase 1 (you cannot show a UI for runs that
don't persist).

---

### IMP-10 — Service-to-service auth on the A2A call
- **Status**: TODO
- **Why**: Today the service POSTs to the supervisor with no auth header.
  Acceptable for dev; unacceptable for prod. Rest of CAIPE has OIDC/JWT;
  this service is the outlier. Open Q #4 in `NOTES.md`.
- **Approach**:
  1. Reuse the OIDC issuer CAIPE already uses (`OIDC_ISSUER`).
  2. OAuth2 client-credentials flow → `services/auth.py` mints / caches a
     JWT, refreshes before expiry.
  3. `a2a_client.py` attaches `Authorization: Bearer <jwt>`.
  4. Supervisor side: should already accept JWTs; if not, that's a separate
     upstream PR (capture as IMP).
- **Touches**: `config.py`, `services/auth.py` (new), `services/a2a_client.py`,
  `tests/test_auth.py` (new).

---

### IMP-11 — UI: read-only "Autonomous Tasks" view in CAIPE UI
- **Status**: TODO
- **Why**: First step of the END GOAL. Get the data on screen before adding
  CRUD. Lets users see what's scheduled and the run history without changing
  any persistence shape.
- **Approach**:
  1. New page `ui/src/app/(app)/autonomous/page.tsx` (also add to top nav).
  2. New Next.js API proxy `ui/src/app/api/autonomous/[...path]/route.ts`
     forwarding to `AUTONOMOUS_AGENTS_URL` env var (default `http://localhost:8002`).
  3. Two panels:
     - "Scheduled tasks" → `GET /api/v1/tasks`
     - "Run history" → `GET /api/v1/runs` (paginated)
  4. Manual run button → `POST /api/v1/tasks/{id}/run`.
- **Touches**: `ui/src/app/(app)/autonomous/page.tsx` (new),
  `ui/src/app/api/autonomous/[...path]/route.ts` (new),
  `ui/src/components/autonomous/*` (new),
  navigation config.
- **Depends on**: IMP-01 (persistence) so runs survive a UI page refresh
  across service restarts.

---

### IMP-12 — UI: create / edit / disable autonomous tasks
- **Status**: BACKEND DONE; UI pending in PR B
- **Backend shipped on**: branch `prebuild/feat/autonomous-agents-task-crud`
  - `services/task_store.py` (Protocol + InMemory + Mongo + factory)
  - `routes/tasks.py` full CRUD: `POST /tasks`, `GET /tasks/{id}`,
    `PUT /tasks/{id}`, `DELETE /tasks/{id}`.
  - Scheduler hot-reload via `scheduler.register_task` /
    `unregister_task`; webhook hot-reload via
    `webhooks.register_webhook_task` / `unregister_webhook_task`.
  - `main.py` lifespan seeds the TaskStore from `config.yaml` on
    startup but treats existing rows as authoritative -- live edits
    survive restarts when MongoDB is configured.
  - 51 new tests across `test_task_store.py`, `test_mongo_task_store.py`,
    `test_task_store_factory.py`, `test_scheduler_hot_reload.py`,
    `test_tasks_crud_route.py`.
  - Auth (IMP-10) is **deliberately** not bundled here -- the UI
    proxy enforces session auth in PR B; the autonomous-agents
    service itself is still localhost-only.
- **Remaining (PR B)**: Next.js page, API proxy, form dialog. See
  `prebuild/feat/autonomous-agents-ui-tab`.

---

### IMP-13 — Surface autonomous runs in UI conversations (`source: "autonomous"`)
- **Status**: TODO
- **Why**: Operations folks live in the chat history. Autonomous runs that
  never appear there are invisible. Tagging them lets a single filter show
  "what did the autonomous agent do today?" without a separate UI.
- **Approach**:
  1. Run records (Mongo, IMP-01) include `conversation_id` + the same fields
     used by the existing chat history.
  2. Optionally write the prompt + response into the existing `conversations`
     collection with metadata `{ source: "autonomous", task_id }`.
  3. UI filter chip "Autonomous only" on the chat history view.
- **Touches**: `services/run_store.py`, `ui/src/app/(app)/chat/*`.
- **Depends on**: IMP-01.

---

### IMP-14 — Tie autonomous tasks to Skills / TaskConfigs (not raw prompts)
- **Status**: TODO
- **Why**: Today a task = `agent + raw prompt`. The UI already has a
  Skills/Task-Builder system for authoring multi-step workflows.
  Letting an autonomous task reference a saved Skill is the killer UX:
  "schedule the 'Review open PRs' Skill to run every weekday at 09:00 UTC."
  Authoring stays in one place; autonomous_agents becomes the *runtime*.
- **Approach**:
  1. Extend `TaskDefinition` to support either:
     - `prompt: str` (current), or
     - `skill_id: str` referencing a saved Skill / TaskConfig.
  2. When `skill_id` is set, `_execute_task` loads the skill and either
     - sends each step as a separate A2A call (multi-step), or
     - sends the full step list as a single structured payload (if the
       supervisor learns to accept it).
  3. UI: in IMP-12's task form, the prompt textarea becomes a
     "Prompt or Skill" picker.
- **Touches**: `models.py`, `services/a2a_client.py`, `scheduler.py`, UI.
- **Depends on**: IMP-12.

---

## Phase 3 — Scale & resilience (only when needed)

Don't do these until you have a real reason. Premature.

---

### IMP-15 — Persistent APScheduler jobstore + leader election
- **Status**: TODO
- **Why**: Currently runs as a single replica with in-memory jobstore. If you
  ever want HA (>1 replica), every cron job will fire on every replica.
- **Approach**: switch APScheduler to a Mongo/Redis jobstore *and* implement
  leader election (e.g. distributed lock on Mongo with a TTL). Until that's
  in place, helm chart should set `replicaCount: 1` and document why.
- **Touches**: `scheduler.py`, helm chart (when one exists), `README.md`.

---

_(IMP-16 — completed; see Done section.)_

---

### IMP-17 — Prometheus metrics
- **Status**: TODO
- **Why**: Observability gap. Once this runs in prod we'll want
  `triggers_total{trigger_type, task_id}`,
  `runs_total{task_id, status}`,
  `run_duration_seconds{task_id}` etc.
- **Approach**: `prometheus-client` + `/metrics` endpoint. FastAPI
  middleware for HTTP-level counters.
- **Touches**: `main.py`, `scheduler.py`, `pyproject.toml`.

---

## Done

_Short audit trail of completed items. Newest first._

### IMP-16 — Circuit breaker around the supervisor A2A call
- **Shipped on**: branch `prebuild/feat/autonomous-agents-circuit-breaker`
- **What landed**:
  - New `services/circuit_breaker.py` -- self-contained
    `CircuitBreaker` class with the canonical CLOSED → OPEN →
    HALF_OPEN state machine, keyed per supervisor URL so a single
    bad URL can't poison healthy ones. `CircuitBreakerOpenError`
    surfaces both the URL and remaining cooldown so failed-run
    rows show an actionable message instead of a generic timeout.
  - Integration with `services/a2a_client.invoke_agent`:
    `before_call` gates the connection (no socket opened when
    OPEN), `record_success` closes the breaker on a 2xx response,
    `record_failure` is called *only after* the tenacity retry
    budget is exhausted -- a flaky request that succeeds on retry
    leaves the breaker untouched. 4xx responses are caller-fault
    (matching `_is_retryable_exception`) and never count toward the
    trip threshold so a misconfigured task can't self-DoS.
  - Three new `Settings`:
    `CIRCUIT_BREAKER_ENABLED` (default `True`, kill-switch),
    `CIRCUIT_BREAKER_FAILURE_THRESHOLD` (default `5`, consecutive
    post-retry failures that trip the breaker),
    `CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default `30`, duration in
    OPEN before a HALF_OPEN trial). The cooldown carries the same
    finite-number guard as the `a2a_*` settings.
  - Async-safe: per-URL `asyncio.Lock` guards the state machine
    so concurrent runs can't race the trip / reset transitions.
    Module-level singleton built lazily from `Settings` so test
    overrides are honoured; `reset_circuit_breaker()` is a
    pytest-friendly helper to drop the cache.
  - 17 new tests in `test_circuit_breaker.py` covering the state
    machine (with a fake clock), per-URL isolation, the
    disabled-mode kill-switch, recovery via a successful
    HALF_OPEN trial, the "success on retry doesn't trip"
    contract, and the 4xx-is-not-an-outage contract. Existing
    32-test `test_a2a_client.py` suite updated to reset and
    relax the breaker so the retry tests still pass.

---

### IMP-04 — Container hardening (Dockerfile)
- **Shipped on**: branch `prebuild/feat/autonomous-agents-dockerfile-hardening`
- **What landed**:
  - `Dockerfile` is now a true two-stage build matching the pattern
    used by `dynamic_agents/build/Dockerfile`. Builder stage
    (`ghcr.io/astral-sh/uv:python3.13-bookworm-slim`) resolves the
    venv with `uv sync --locked --no-dev`. Runtime stage
    (`python:3.13-slim-bookworm`) contains only the venv + source +
    config — no `uv`, no apt, no build toolchain. Both base images
    are pinned to specific Debian variants for reproducible builds.
  - Source code, `pyproject.toml`/`uv.lock`, and `config.yaml` are
    copied from the builder as **root-owned** with default 644 perms.
    The `app` user can read them but can't modify them, even when the
    container is run without `--read-only`. Only `/app/.venv` is
    chowned to `app:app` (and it isn't mutated during normal
    operation).
  - System `app` user (UID/GID `1001`) created with
    `--no-create-home --shell /usr/sbin/nologin`. `USER app:app` is
    set in the runtime stage so the container is non-root by default
    without any extra runtime flags.
  - Build args `APP_UID` / `APP_GID` let downstream chart authors
    pin the IDs to whatever their cluster's PSS expects.
  - README "Run with Docker" section now ships the recommended
    runtime flag set as **defence in depth** on top of the non-root
    default: `--user app:app --read-only --tmpfs /tmp
    --security-opt=no-new-privileges --cap-drop=ALL --pids-limit=256
    --memory=512m --cpus=1`. Each flag carries a one-line rationale
    so reviewers don't have to guess why it's there.

---

### IMP-03 / IMP-07 — Webhook hardening (global secret fallback + replay protection)
- **Shipped on**: branch `prebuild/feat/autonomous-agents-webhook-hardening`
- **What landed**:
  - `routes/webhooks.py` — extracted `_resolve_secret`,
    `_validate_timestamp`, `_expected_signature` helpers.
    Per-task `trigger.secret` still wins; in its absence the
    service falls back to `settings.webhook_secret` (IMP-03).
    Log line on signature failure includes a `secret_source`
    tag (`"task" | "global"`) but never the secret itself.
  - `Settings.webhook_replay_window_seconds` (default `0` = disabled
    so existing GitHub-style senders keep working). When `> 0`,
    signed webhooks must include `X-Webhook-Timestamp` and the
    HMAC is computed over `f"{ts}.{body}"` so the timestamp is
    bound into the MAC (Slack-style). Requests outside `±N`
    seconds (past *or* future) are rejected.
  - Failed signature responses return only the generic message
    `"Invalid webhook signature"` — no expected-signature echo
    (would be a forgery oracle).
  - 15 new tests in `tests/test_webhooks.py` covering: per-task
    vs global secret precedence, no-secret-anywhere flow,
    replay-window disabled keeps body-only signing,
    replay-window enabled requires + signs `ts.body`, too-old
    and too-future timestamps rejected, non-numeric timestamp
    returns 400, signature error doesn't leak the expected
    value, helper round-trip with the endpoint.
  - README documents both signature contracts and the migration
    path for replay protection.

---

### IMP-09 — Rename private import `_execute_task`
- **Shipped on**: branch `prebuild/feat/autonomous-agents-task-crud`
- **What landed**: `scheduler._execute_task` promoted to public
  `execute_task`. `routes/tasks.py` and `tests/test_scheduler_run_store.py`
  updated to import the new name. Documented in the function
  docstring why the public name must stay.

---

### IMP-05 — CORS safety check in `Settings`
- **Shipped on**: branch `prebuild/feat/autonomous-agents-task-crud`
- **What landed**: Two pydantic validators on `Settings.cors_origins`:
  - A `mode="before"` pre-validator that accepts the comma-separated
    string form (`CORS_ORIGINS=http://a,http://b`) operators
    routinely paste into `.env`, in addition to JSON lists.
  - A post-validator that rejects any list containing `"*"` -- that
    combination plus the FastAPI default `allow_credentials=True`
    is a CORS spec violation that browsers refuse and misconfigured
    gateways may dangerously allow.
  - 5 new unit tests in `test_config.py` covering both behaviours.

---

### IMP-11/12 (backend) — TaskStore + CRUD endpoints + scheduler hot-reload
- **Shipped on**: branch `prebuild/feat/autonomous-agents-task-crud`
- **What landed**:
  - `services/task_store.py` -- `TaskStore` Protocol with
    `InMemoryTaskStore` and `MongoTaskStore` implementations and a
    `create_task_store(...)` factory selecting the backend from
    settings. `TaskAlreadyExistsError` / `TaskNotFoundError`
    custom exceptions translate cleanly to HTTP 409 / 404.
  - `scheduler.register_task` / `unregister_task` helpers so CRUD
    operations hot-reload APScheduler without a restart.
    `register_tasks` now guards `start()` behind a `running` check.
  - `webhooks.register_webhook_task` / `unregister_webhook_task`
    helpers for the same hot-reload contract on the webhook side.
  - `routes/tasks.py` -- full CRUD: `GET /tasks`, `GET /tasks/{id}`,
    `POST /tasks` (201), `PUT /tasks/{id}`, `DELETE /tasks/{id}`
    (204). The path id wins over body id on PUT to prevent
    accidental renames.
  - `Settings.mongodb_tasks_collection` (default
    `autonomous_tasks`) and `main.py` lifespan that seeds the store
    from `config.yaml` while preserving previously-persisted rows.
  - 51 new tests across 5 files (`test_task_store.py`,
    `test_mongo_task_store.py`, `test_task_store_factory.py`,
    `test_scheduler_hot_reload.py`, `test_tasks_crud_route.py`).
    All 152 tests pass; ruff clean.

---

### IMP-02 — Retries + configurable timeout in `a2a_client.py`
- **Shipped on**: branch `prebuild/feat/autonomous-agents-a2a-retries`
- **What landed**:
  - `tenacity==9.1.4` added as a runtime dependency.
  - `Settings` extended with `A2A_TIMEOUT_SECONDS` (default 300),
    `A2A_MAX_RETRIES` (default 3), `A2A_RETRY_BACKOFF_INITIAL_SECONDS`
    (default 1.0) and `A2A_RETRY_BACKOFF_MAX_SECONDS` (default 30.0),
    all validated to reject non-positive / inf / NaN values.
  - `TaskDefinition` gained optional `timeout_seconds` and
    `max_retries` per-task overrides; the scheduler forwards them
    through to `invoke_agent`.
  - `services/a2a_client.invoke_agent` now wraps the HTTP call in
    `tenacity.AsyncRetrying` with `wait_exponential_jitter`. The
    retry classifier (`_is_retryable_exception`) retries
    `httpx.TransportError` and 5xx `HTTPStatusError`s only — 4xx
    propagates immediately, as does any non-httpx exception.
  - Each retry logs at `WARNING` via `before_sleep_log`, so retries
    are visible in operator logs.
  - 22 new unit tests across `test_a2a_client.py`, `test_config.py`,
    and `test_models.py` covering the classifier, the attempt
    budget, per-call overrides, and the A2A error-envelope path.
  - README documents the new env vars, per-task overrides, and the
    retry classification table.

---

### IMP-01 — Persist run history to MongoDB
- **Shipped on**: branch `prebuild/feat/autonomous-agents-mongo-store`
- **What landed**:
  - New `services/run_store.py` exposing a `RunStore` Protocol with
    two implementations: `InMemoryRunStore` (legacy bounded deque
    behaviour) and `MongoRunStore` (motor / async).
  - `create_run_store(...)` factory selects the backend from settings;
    partial Mongo config falls back to in-memory.
  - `Settings` extended with `MONGODB_URI`, `MONGODB_DATABASE`,
    `MONGODB_COLLECTION` (default `autonomous_runs`), and
    `RUN_HISTORY_MAXLEN` (default 500).
  - `scheduler._execute_task` records RUNNING and terminal
    (SUCCESS|FAILED) states through the store via upsert-by-`run_id`.
  - `routes/tasks.py` reads runs from the store directly; the legacy
    deque is gone.
  - `main.py` lifespan builds the store, calls `ensure_indexes()`
    when applicable, and injects it into the scheduler module.
  - Mongo indexes: unique on `run_id`, compound on
    `(task_id ASC, started_at DESC)`.
  - 38 new unit tests across 4 files (`test_run_store.py`,
    `test_mongo_run_store.py`, `test_run_store_factory.py`,
    `test_scheduler_run_store.py`); MongoDB tested via
    `mongomock-motor` so no real DB needed.
  - README documents the persistence model and the new env vars.

---

_Last updated: 2026-04-19_
