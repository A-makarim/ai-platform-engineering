# Autonomous Agents — Working Log

A running record of what has been built, what's in flight, what we learned along the way, and what's coming next. This file is **append-only**; new entries go at the top under the relevant section.

> Why this file exists: the autonomous-agents feature ships in many small PRs against the umbrella branch `prebuild/feat/autonomous-agents`. With reviews, dogfood feedback, and parallel work on multiple phases, it's easy to lose the thread of "why did we pick X over Y?" or "what state is each piece in?" — this document is the answer.

---

## Quick Status Dashboard

| Phase | Title                                                                 | Status        | Branch                                                                  | PR    |
|-------|-----------------------------------------------------------------------|---------------|-------------------------------------------------------------------------|-------|
| 0     | Spec — conversational UX for autonomous tasks                         | ✅ Merged     | `prebuild/feat/autonomous-agents-conversational-ux`                      | #15   |
| 1     | Per-task chat thread + supervisor pre-flight ack                      | 🚧 Starting   | `prebuild/feat/autonomous-agents-preflight-and-thread` (planned)         | TBD   |
| 2     | UI rendering of per-task thread with upcoming-run indicator           | ⏳ Queued     | `prebuild/feat/autonomous-agents-thread-ui` (planned)                    | TBD   |
| 3     | Chat-driven task author sub-agent (second creation door)              | ⏳ Queued     | `prebuild/feat/autonomous-agents-task-author` (planned)                  | TBD   |

Plus loose bug-fix commits going directly to umbrella as we discover them in dogfood:

| Commit     | Title                                                                            |
|------------|----------------------------------------------------------------------------------|
| 8ff5ff53   | fix(supervisor): harden prompt_config loader (utf-8, env override, dict guard)   |
| 3ffb2ba7   | fix(autonomous-agents): use UUIDv5 contextId for supervisor A2A calls            |

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

## Phase 1 — Per-task chat thread + supervisor pre-flight ack (Starting)

**Branch (planned)**: `prebuild/feat/autonomous-agents-preflight-and-thread`
**Touches**: `ai_platform_engineering/autonomous_agents/` and `ai_platform_engineering/multi_agents/platform_engineer/`
**Feature flag**: `AUTONOMOUS_PREFLIGHT_ENABLED` (default on)

What this PR delivers (mapped to spec FRs):

- **FR-001, FR-005, AD-003**: Supervisor accepts `metadata.preflight: true` on `message/send`. When set, it routes via the LLM but stops before invoking side-effecting tools, returning a structured `Acknowledgement` payload `{routed_to, tools, credentials_status, dry_run_summary, ack_status, ack_detail}`.
- **FR-002, FR-003, FR-004**: Autonomous-agents service calls pre-flight on every successful `POST /api/v1/tasks` and on `PUT /api/v1/tasks/{id}` when prompt/agent/trigger changed. Result persisted on the task as `last_ack`. Failures are warnings, not errors — task is still created.
- **FR-006, FR-007, FR-008, FR-009**: Chat publisher rewrites the conversation_id derivation to UUIDv5(`autonomous-task:<task_id>`) and writes per-message `metadata.kind` from the spec's enumeration (`creation_intent`, `preflight_ack`, `next_run_marker`, `run_request`, `run_response`, `run_error`, `task_updated`, `task_disabled`, `task_deleted`, `task_reauthored`). Defaults to enabled but no-ops gracefully when Mongo isn't reachable.
- **FR-018, FR-019**: Unit tests + integration test through the existing FakeSupervisor pattern. Native-dev (no Mongo, no Docker) cold start stays under 5 s.

Not in this PR (deferred to later phases):

- UI rendering of `last_ack` and per-task threads (Phase 2)
- Live `next_run` countdown (Phase 2)
- Chat-driven task author sub-agent (Phase 3)

---

## Phase 2 — UI rendering of per-task thread (Queued)

**Branch (planned)**: `prebuild/feat/autonomous-agents-thread-ui`
**Touches**: `ui/`
**Feature flag**: `NEXT_PUBLIC_AUTONOMOUS_THREAD_VIEW` (default on)

Renders the data structures Phase 1 produces:

- "Ack OK / Ack failed / Ack pending" badge per task row (FR-003)
- Click row → opens the per-task chat thread with all `metadata.kind` message types rendered with type-specific affordances (FR-007)
- Live "next run at HH:MM UTC" indicator with absolute + relative timestamps (FR-010, FR-011, FR-012)
- Polling at 30 s (configurable via env) — push (SSE/WS) deferred per OQ-2

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
