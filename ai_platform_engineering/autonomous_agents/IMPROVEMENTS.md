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

### IMP-03 — Wire `WEBHOOK_SECRET` env var as a global fallback
- **Status**: TODO
- **Why**: Env var is documented in `README.md` but `routes/webhooks.py`
  never reads it — only honours per-task `secret`. Pick one: implement or remove.
- **Approach**: in `validate_signature`, fall back to `settings.webhook_secret`
  when the task has no per-task secret. Log clearly which one was used (don't log
  the secret itself).
- **Touches**: `routes/webhooks.py`, `tests/test_webhooks.py` (new).

---

### IMP-04 — Container hardening (Dockerfile)
- **Status**: TODO
- **Why**: Image runs as `root`. Violates the project-wide container rule.
- **Approach**:
  - Add `RUN groupadd -r app && useradd -r -g app app && chown -R app:app /app`
  - `USER app`
  - Document `--security-opt=no-new-privileges` and `--read-only` in the run
    examples in the README.
- **Touches**: `Dockerfile`, `README.md`.

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

### IMP-07 — Webhook replay protection
- **Status**: TODO
- **Why**: HMAC verifies authenticity but a captured payload can be replayed
  forever. GitHub-style.
- **Approach**: require a `timestamp` claim in the payload (or require an
  `X-Hub-Timestamp` header), reject if `now - timestamp > 5 min`.
  Optional nonce table for hard guarantees later.
- **Touches**: `routes/webhooks.py`, `tests/test_webhooks.py`.

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

### IMP-16 — Circuit breaker around the supervisor call
- **Status**: TODO
- **Why**: If the supervisor is broken, fire-and-forget tasks just keep
  hammering it. A circuit breaker fails fast and gives the supervisor room
  to recover.
- **Approach**: lightweight `purgatory` or roll your own state machine in
  `a2a_client.py` keyed by supervisor URL.
- **Touches**: `services/a2a_client.py`.

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
