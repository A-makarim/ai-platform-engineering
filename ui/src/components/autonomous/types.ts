// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

/**
 * Types for the autonomous-agents UI surface.
 *
 * These mirror the wire shape produced by `_serialize_task` in
 * `routes/tasks.py` and the `TaskRun` Pydantic model. Keep them in
 * lockstep -- the FastAPI service is the source of truth.
 */

export type TriggerType = 'cron' | 'interval' | 'webhook';

export type TaskStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped';

export interface CronTrigger {
  type: 'cron';
  schedule: string;
}

export interface IntervalTrigger {
  type: 'interval';
  seconds?: number | null;
  minutes?: number | null;
  hours?: number | null;
}

export interface WebhookTrigger {
  type: 'webhook';
  /**
   * Optional HMAC secret. The backend NEVER echoes the secret on
   * read paths -- ``_serialize_trigger`` in ``routes/tasks.py``
   * strips the value and replaces it with ``has_secret`` (below) so
   * any UI/XSS leak only learns whether one is configured, not the
   * value itself. Outbound writes (POST/PUT) MAY include this field
   * to set or rotate the secret.
   */
  secret?: string | null;
  /**
   * Read-only boolean returned by the backend indicating whether
   * a secret is currently configured. Used by the edit dialog to
   * surface a "Replace secret" affordance instead of revealing the
   * value.
   */
  has_secret?: boolean;
}

export type Trigger = CronTrigger | IntervalTrigger | WebhookTrigger;

export interface AutonomousTask {
  id: string;
  name: string;
  description?: string | null;
  agent: string;
  prompt: string;
  llm_provider?: string | null;
  trigger: Trigger;
  enabled: boolean;
  timeout_seconds?: number | null;
  max_retries?: number | null;
  /** ISO-8601 string from APScheduler; null for webhook/disabled. */
  next_run?: string | null;
}

export interface TaskRun {
  run_id: string;
  task_id: string;
  task_name: string;
  status: TaskStatus;
  started_at: string;
  finished_at?: string | null;
  response_preview?: string | null;
  error?: string | null;
  /**
   * Deterministic UUID derived from ``run_id`` by the autonomous
   * service when chat-history publishing is enabled (IMP-13). Lets
   * the run-row UI deep-link straight to ``/chat/<conversation_id>``.
   * Null when chat publishing is disabled or when the run pre-dates
   * the IMP-13 ship.
   */
  conversation_id?: string | null;
}

/**
 * Form-level shape used by `TaskFormDialog`. Distinct from
 * `AutonomousTask` because the form needs free-text inputs
 * (e.g. "minutes" as a string before parsing) and lets us version
 * the form schema without churning the API contract.
 */
export interface TaskFormState {
  id: string;
  name: string;
  description: string;
  agent: string;
  prompt: string;
  llm_provider: string;
  enabled: boolean;
  triggerType: TriggerType;
  cronSchedule: string;
  intervalSeconds: string;
  intervalMinutes: string;
  intervalHours: string;
  webhookSecret: string;
  timeoutSeconds: string;
  maxRetries: string;
}
