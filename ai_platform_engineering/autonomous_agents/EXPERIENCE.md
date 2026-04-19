# Autonomous Agents — Working Log

A running record of what has been built, what's in flight, what we learned along the way, and what's coming next. This file is **append-only**; new entries go at the top under the relevant section.

> Why this file exists: the autonomous-agents feature ships in many small PRs against the umbrella branch `prebuild/feat/autonomous-agents`. With reviews, dogfood feedback, and parallel work on multiple phases, it's easy to lose the thread of "why did we pick X over Y?" or "what state is each piece in?" — this document is the answer.

---

## Quick Status Dashboard

| Phase | Title                                                                 | Status                         |
|-------|-----------------------------------------------------------------------|--------------------------------|
| 0     | Spec — conversational UX for autonomous tasks                         | ✅ Merged (PR #15)            |
| 1     | Per-task chat thread + supervisor pre-flight ack                      | ✅ Done on umbrella           |
| 2     | UI rendering of per-task thread with upcoming-run indicator           | ✅ Done on umbrella           |
| 3     | Chat-driven task author sub-agent (second creation door)              | ⏳ Deferred until testing      |

Per-#099-PR-as-PR was abandoned in favour of direct-to-umbrella commits with one batch sign-off at end-of-batch (operator preference for speed). EXPERIENCE.md is the running log; bots review the umbrella push at the end.

### Direct-to-umbrella commits (chronological)

| Commit     | Title                                                                              |
|------------|------------------------------------------------------------------------------------|
| `bde20bbb` | fix(autonomous-agents): use UUIDv5 contextId for supervisor A2A calls              |
| `6c19d138` | fix(supervisor): harden prompt_config loader (utf-8, env override, dict guard)     |
| `538064a9` | docs(autonomous-agents): add EXPERIENCE.md working log                             |
| `3a5b1859` | feat(autonomous-agents): supervisor preflight ack on task create/update            |
| `ff6c9617` | feat(autonomous-agents): per-task chat threads with typed message kinds            |
| `3468c7de` | test(autonomous-agents): unit tests for preflight + per-task chat threads          |
| `07b0f739` | feat(ui+autonomous-agents): pre-flight badge, per-task chat link, live next-run    |

---

## Architecture Decisions (Living)

These are the non-obvious calls we've made. Each entry says what + why; if we change our mind later, append a new entry instead of rewriting.

### AD-001 — Single supervisor handles both real-time chat and scheduled autonomous runs

**Status**: in force.
**What**: We did NOT split out a dedicated "scheduling supervisor" even though that was an intuitive structure suggestion. The same `caipe-supervisor` (single-node mode by default) serves UI chat requests and autonomous-task requests. Differentiation is in the request metadata (`source: "autonomous"`, `agent_hint: "github"`), not in the deployment.
**Why**: Two reasons. (a) The supervisor is stateless per-request — it doesn't hold a "what's coming next" calendar — so a separate scheduler-supervisor would add a process and a network hop with no behavioural difference. (b) Halving the operational surface keeps the cloud-PC native-dev story workable (we'd need to spin up two processes for any local test).

### AD-002 — Per-task chat thread keyed by deterministic UUIDv5

**Status**: in force.
**What**: Each autonomous task owns exactly one chat conversation. Conversation id = `uuid5(NAMESPACE_URL, f"autonomous-task:{task_id}")`. All runs, acks, edits, and status changes for that task append to that one thread.
**Why**: (a) One stable identifier per task simplifies UI lookups, audit trails, and de-dup. (b) Deterministic so the autonomous-agents service and the supervisor can both compute it without coordination. (c) Mirrors the contextId UUIDv5 trick we already use for A2A requests (see commit `3ffb2ba7`), keeping a single derivation rule for "task identity in the system".

### AD-003 — Pre-flight as a flag on `message/send`, not a new method (default)

**Status**: pending review (Open Question OQ-1 in spec #099).
**What**: When the autonomous-agents service creates a task, it sends a normal `message/send` JSON-RPC call to the supervisor with `metadata.preflight: true`. The supervisor short-circuits before invoking any side-effecting tools and returns a structured acknowledgement. Default unless reviewers prefer a dedicated `tasks/preflight` method.
**Why**: Smaller protocol blast radius — no SDK bump, no new method to document or version. Reviewers can override in the spec PR's discussion.

### AD-004 — Form path stays alongside the chat author, not replaced

**Status**: in force.
**What**: Phase 3 adds a "Describe a task" button next to the existing "+ New task" form button. Both submit the same `POST /api/v1/tasks` payload. Form remains the system of record for task fields.
**Why**: Power users prefer the form (fewer turns to a saved task). Non-power users prefer the chat. Two doors, one room.

### AD-005 — Native-dev parity is a hard requirement, not a nice-to-have

**Status**: in force.
**What**: Every phase MUST work in the in-memory store mode (no Mongo, no Docker) so contributors on cloud PCs without nested virtualization can develop and test. Mongo-dependent capabilities degrade gracefully (log + disable) rather than crash startup.
**Why**: Demonstrated by getting end-to-end working on a cloud PC where Docker won't run. Without this rule, the second-time-setup experience is "install Docker, find out nested virt is off, give up" and contributions stall.

---

## Phase 0 — Spec (Done)

**Branch**: `prebuild/feat/autonomous-agents-conversational-ux`
**Merged as**: PR #15 → `eadbae23` (merge), `f2146868` (commit) on umbrella.
**Doc**: `docs/docs/specs/099-autonomous-task-conversational-ux/spec.md` (280 lines).

Distilled three operator pain points from a hands-on walkthrough into one cohesive UX shift (every task is a conversation) and four phased PRs. Includes 4 user stories, 19 functional + 4 non-functional requirements, A2A protocol contract, phasing into 4 independently-shippable PRs, native-dev parity requirements, and an open-questions section for reviewer input.

Operator pain points:
1. **No confidence at creation time** — users save a 9 PM cron task and go to bed hoping it works.
2. **No visibility into upcoming runs** — chat sidebar only shows completed runs.
3. **The `agent` field forces tribal knowledge** — users have to know that `github` handles GitHub etc.

---

## Bug Fixes Landed Direct-to-Umbrella

Discovered during native-Windows dogfood; small enough to skip the per-PR ceremony.

### `3ffb2ba7` — UUIDv5 contextId for supervisor A2A calls

`a2a-sdk` 0.3+ rejects the previous `"autonomous-{task_id}"` string with *"Invalid context_id: ... is not a valid UUID."* Fix derives a deterministic UUIDv5 from the task_id (`NAMESPACE_URL`, `"autonomous-task:<task_id>"`) so the supervisor's checkpointer keeps exactly one conversation thread per task across runs while satisfying the SDK's UUID contract. Same derivation reused by AD-002 for chat conversations.

### `8ff5ff53` — Harden `prompt_config` loader (utf-8, env override, dict guard)

Three small but distinct robustness fixes in `load_prompt_config()`:

1. **UTF-8 encoding on `open()`** so the deep_agent prompt YAML (which contains emoji/unicode) loads on Windows. The default cp1252 codec raises *"UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d"* on the first non-ASCII byte and crashes supervisor startup before uvicorn binds.
2. **`PROMPT_CONFIG_PATH` env-var override** so operators can point at any YAML without symlinking or copying. Matches the docker-compose pattern that already mounts the chosen file at `/app/prompt_config.yaml`.
3. **Defensive `isinstance(loaded, dict)` guard** so a malformed stub (e.g. the one-line path-string at the repo root that `yaml.safe_load` returns as a bare `str`) no longer crashes `prompts.py` with *"AttributeError: 'str' object has no attribute 'keys'"*.

---

## Phase 1 — Per-task chat thread + supervisor pre-flight ack (Done — backend)

**Branch**: direct on `prebuild/feat/autonomous-agents` (umbrella).
**Commits**: `3a5b1859` (preflight) + `ff6c9617` (per-task threads) + `3468c7de` (tests).
**Tests**: 213/213 passing on Windows native + uv (mongomock-dependent modules skipped — pre-existing dev-only deps not synced in this venv; unrelated to Phase 1).

What landed (mapped to spec FRs):

- **FR-001, FR-005, AD-003**: Supervisor `AIPlatformEngineerA2AExecutor` detects `metadata.preflight=true` on the inbound A2A message and short-circuits with a structured `Acknowledgement` payload before invoking any side-effecting tool. Light preflight only (agent loaded? yes/no); heavy probes (real credential validation per agent) deliberately deferred — see `_build_preflight_ack` docstring and OQ-1.
- **FR-002, FR-003, FR-004**: Autonomous-agents service calls pre-flight in the background after every successful `POST /api/v1/tasks` and after `PUT` when prompt/agent/llm_provider changed (toggle-enabled doesn't burn a preflight call). Result persisted on `TaskDefinition.last_ack`; failures are warnings (`pending`/`warn`/`failed` ack_status) not exceptions. CRUD routes scrub any client-supplied `last_ack` so a malicious or buggy client cannot pre-populate a green badge.
- **FR-006, FR-007, FR-008, FR-009**: Conversation id is now `uuid5(NS, "task:" + task_id)` — per-task, deterministic, and matches the contextId derivation in `services/a2a_client.py`. Each message carries `metadata.kind` from the typed enumeration. Phase 1 wires `creation_intent` (POST /tasks), `preflight_ack` (after every preflight call), `run_request`, and `run_response`/`run_error` (per scheduled run). Remaining kinds (`task_updated`, `task_disabled`, `task_deleted`, `task_reauthored`, `next_run_marker`) reserved for Phase 2.
- **FR-018, FR-019**: 22 new tests (12 preflight + 10 chat-thread). Native-dev parity preserved — Mongo path stays opt-in via `CHAT_HISTORY_PUBLISH_ENABLED`, cold start unchanged.

Not in Phase 1 (lands in Phase 2):

- UI rendering of `last_ack` (badge per task row, color-coded)
- UI rendering of per-task chat thread (kind-aware affordances)
- Live `next_run` indicator with absolute + relative timestamps

Open Question outcomes:

- **OQ-1**: Implemented as the flag (`metadata.preflight: true`) on `message/send` rather than a new method. Smaller protocol blast radius, no SDK bump, no documentation rewrite. Reviewers can override in Phase 2 review and we'll refactor — the autonomous-agents preflight client is the only consumer so the cost of switching is small.
- **OQ-3**: `task-author` sub-agent prompt config will live at `prompt_config.task_author_agent.yaml` per existing convention (decision recorded for Phase 3).

---

## Phase 2 — UI rendering of per-task thread (Done)

**Commit**: `07b0f739`.
**Tests**: 12/12 UI Jest tests passing (3 existing + 9 new for TaskList).

What landed:

- **Per-row Ack badge** mapping `Acknowledgement.ack_status` to colour-coded label + icon (green check for "ok", yellow triangle for "warn", red x for "failed", grey spinner for "pending"|absent). Tooltip carries `ack_detail` and `dry_run_summary` so operators see the cause without opening the chat thread (FR-003).
- **Thread deep-link button** to `/chat/<chat_conversation_id>` opened in a new tab. The `chat_conversation_id` is server-derived (UUIDv5, exposed on the task wire) so the link works even before the first run has fired (FR-006 / Story 2).
- **Better next-run rendering**: absolute timestamp + relative hint (`"in 4h"` / `"5m ago"`). Tooltip carries the full ISO8601 timestamp for precise inspection (FR-010 / FR-012).
- **30-second silent polling** of `/api/autonomous/api/v1/tasks` so the badge updates after a background preflight resolves and the next-run countdown stays accurate without manual refresh. Polling failures are silent — UI keeps the last successful task list visible. Push (SSE/WS) deferred per OQ-2 (FR-011).

Deferred to a future Phase 2.5 if dogfood demands:

- **Custom thread view** with kind-aware rendering for each `metadata.kind` message type. Today the existing `/chat/[id]` route renders the messages as a normal conversation; the new typed messages still display correctly because they're still `role: 'user'|'assistant'` rows with sensible content. The UI just doesn't yet know to render the `preflight_ack` payload as a structured card vs. a text bubble. Trivial follow-up; not needed for the testable end-product the operator asked for.

---

## Phase 3 — Chat-driven task author sub-agent (Queued)

**Branch (planned)**: `prebuild/feat/autonomous-agents-task-author`
**Touches**: `ai_platform_engineering/multi_agents/platform_engineer/`, `ai_platform_engineering/autonomous_agents/`, `ui/`
**Feature flag**: `NEXT_PUBLIC_AUTONOMOUS_AUTHOR_BOT` (default off → on after dogfood)

- New supervisor sub-agent `task-author` with tools: `list_available_agents`, `validate_cron`, `dry_run_preflight`, `create_task`, `update_task`, `delete_task`, `trigger_task_now` (FR-013).
- All `*_task` tools call the existing autonomous-agents REST API rather than poking storage directly (FR-013).
- New "Describe a task" button on the Autonomous tab opens a chat panel scoped to `task-author` (FR-014).
- Bot proactively asks for missing credentials, never fabricates them (FR-016).

---

## Conventions for This Doc

- **Append-only** — when a decision changes, add a new entry, don't rewrite the old one. The history of "why" matters more than always-up-to-date paragraphs.
- **Status dashboard at the top** — quick scan of "what's in flight". Keep it updated as PRs land.
- **Each phase section** — what's in scope, what's deferred, mapping to spec FRs.
- **Bug-fix log for direct-to-umbrella commits** — small enough not to warrant per-PR docs, big enough to need a memory anchor.
- **No secrets, no PII, no logs** — same rules as production code. Reference findings, don't paste credentials.
