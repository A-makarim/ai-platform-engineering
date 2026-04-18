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

### IMP-01 — Persist run history to MongoDB
- **Status**: TODO
- **Why**: Today runs live in an in-memory `deque(maxlen=500)` in `scheduler.py`.
  Lost on restart. Chatty interval tasks evict important runs. Audit trail = nil.
  Also blocks the UI integration (the UI needs to query historical runs).
- **Approach**:
  1. Add `MONGODB_URI`, `MONGODB_DATABASE` to `config.py` (optional — fallback
     to deque when unset, so dev still works).
  2. New `services/run_store.py` with `MongoRunStore` + `InMemoryRunStore`
     behind a small `RunStore` Protocol.
  3. `scheduler._execute_task` writes to whichever store is configured.
  4. `routes/tasks.py` reads runs from the store, not the deque directly.
  5. Schema: `{ task_id, run_id, started_at, finished_at, status, prompt,
     response, error, duration_ms, agent, llm_provider }`.
  6. Index on `(task_id, started_at desc)`.
- **Touches**: `config.py`, `scheduler.py`, `routes/tasks.py`,
  `services/run_store.py` (new), `tests/test_run_store.py` (new),
  `pyproject.toml` (`motor`).

---

### IMP-02 — Retries + configurable timeout in `a2a_client.py`
- **Status**: TODO
- **Why**: Hard-coded `httpx.AsyncClient(timeout=300)`, zero retries.
  A transient 502 / brief supervisor restart fails the whole run permanently.
  Once we move to UI users will rightly expect transient failures to recover.
- **Approach**:
  1. Add `A2A_TIMEOUT_SECONDS` and `A2A_MAX_RETRIES` to `Settings`.
  2. Optional per-task `timeout` and `max_retries` overrides in `TaskDefinition`.
  3. Use `tenacity` with exponential backoff (`@retry` only on
     `httpx.HTTPStatusError` for 5xx / `httpx.TransportError`, *not* on 4xx).
  4. Log each attempt with `attempt`/`max_attempts` so retries are observable.
- **Touches**: `config.py`, `models.py`, `services/a2a_client.py`,
  `pyproject.toml` (`tenacity`), `tests/test_a2a_client.py` (new).

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

### IMP-05 — CORS safety check in `Settings`
- **Status**: TODO
- **Why**: Today `allow_credentials=True` + `allow_methods=["*"]` + `allow_headers=["*"]`.
  Fine because `cors_origins=[]` by default. But if anyone later sets
  `CORS_ORIGINS=*` it silently violates the browser CORS spec and creates a
  CSRF surface.
- **Approach**: Pydantic `model_validator` on `Settings` that raises if
  `"*"` is in `cors_origins` while `allow_credentials=True`.
- **Touches**: `config.py`, `tests/test_models.py`.

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

### IMP-09 — Rename private import `_execute_task`
- **Status**: TODO
- **Why**: `routes/tasks.py` imports `_execute_task` from `scheduler.py`.
  The leading underscore signals "private to module"; importing across module
  boundaries breaks the contract.
- **Approach**: rename to `execute_task` (or expose a public alias
  `execute_task = _execute_task`). Update import.
- **Touches**: `scheduler.py`, `routes/tasks.py`.

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
- **Status**: TODO
- **Why**: Second step of the END GOAL. Today tasks are defined in
  `config.yaml` only — no end-user can add one without filesystem access.
- **Approach**:
  1. Move task definitions out of `config.yaml` into Mongo
     (`autonomous_tasks` collection). Keep `config.yaml` as a seed/import.
  2. New endpoints: `POST/PUT/DELETE /api/v1/tasks/{id}` with proper auth
     (IMP-10 must be done first).
  3. UI form covering: trigger type (cron/interval/webhook), cron picker,
     prompt textarea, agent dropdown, llm_provider dropdown, enabled toggle.
  4. On task create/update, scheduler hot-reloads via an internal
     `register_task(task)` + `unregister_task(task_id)` API on the scheduler.
- **Touches**: `models.py`, `services/task_store.py` (new),
  `services/task_loader.py`, `scheduler.py`, `routes/tasks.py`, UI.
- **Depends on**: IMP-01, IMP-10.

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

_(Move completed items here, or just delete them. Keep this section as a
short audit trail.)_

---

_Last updated: 2026-04-18_
