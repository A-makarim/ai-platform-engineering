// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

"use client";

import React, { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

import type { AutonomousTask, TaskFormState, TriggerType } from "./types";

interface TaskFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** When provided we render in "edit" mode. */
  task?: AutonomousTask | null;
  onSubmit: (task: AutonomousTask) => Promise<void>;
}

const EMPTY_FORM: TaskFormState = {
  id: "",
  name: "",
  description: "",
  agent: "",
  prompt: "",
  llm_provider: "",
  enabled: true,
  triggerType: "cron",
  cronSchedule: "0 9 * * *",
  intervalSeconds: "",
  intervalMinutes: "",
  intervalHours: "",
  webhookSecret: "",
  timeoutSeconds: "",
  maxRetries: "",
};

/** Convert API model -> form state. */
function toFormState(task: AutonomousTask | null | undefined): TaskFormState {
  if (!task) return EMPTY_FORM;
  const base: TaskFormState = {
    ...EMPTY_FORM,
    id: task.id,
    name: task.name,
    description: task.description ?? "",
    agent: task.agent ?? "",
    prompt: task.prompt,
    llm_provider: task.llm_provider ?? "",
    enabled: task.enabled,
    triggerType: task.trigger.type,
    timeoutSeconds: task.timeout_seconds == null ? "" : String(task.timeout_seconds),
    maxRetries: task.max_retries == null ? "" : String(task.max_retries),
  };
  if (task.trigger.type === "cron") {
    base.cronSchedule = task.trigger.schedule;
  } else if (task.trigger.type === "interval") {
    base.intervalSeconds = task.trigger.seconds == null ? "" : String(task.trigger.seconds);
    base.intervalMinutes = task.trigger.minutes == null ? "" : String(task.trigger.minutes);
    base.intervalHours = task.trigger.hours == null ? "" : String(task.trigger.hours);
  } else {
    // Backend never echoes the secret on read paths -- only the
    // ``has_secret`` boolean comes back. Leave the form blank so the
    // operator must explicitly type a new value to *change* it; we
    // expose the existing-secret state via placeholder copy below.
    base.webhookSecret = "";
  }
  return base;
}

/**
 * Convert form state -> API model. Returns ``null`` and surfaces a
 * human-readable error string for the caller to display when the
 * input is invalid. Doing this client-side keeps the dialog snappy
 * — server-side validation still runs (Pydantic) and any ``422`` is
 * surfaced in the catch path of ``onSubmit``.
 */
function fromFormState(form: TaskFormState): { task: AutonomousTask } | { error: string } {
  const id = form.id.trim();
  const name = form.name.trim();
  const agent = form.agent.trim();
  const prompt = form.prompt.trim();

  if (!id) return { error: "ID is required." };
  if (!name) return { error: "Name is required." };
  if (!prompt) return { error: "Prompt is required." };
  // Spec #099 FR-001: agent is a HINT, not a hard requirement. When
  // omitted, the supervisor's LLM router picks a sub-agent from the
  // prompt at run time. Empty / whitespace-only values become null on
  // the wire so the backend treats them as "no hint" rather than as a
  // literal sub-agent id named "" (which would always preflight-fail).
  // Lock down to the same character set the FastAPI side accepts in
  // path parameters: letters, digits, dash, underscore. Catches a
  // common foot-gun (spaces) before the request even leaves the
  // browser.
  if (!/^[a-zA-Z0-9_-]+$/.test(id)) {
    return { error: "ID may only contain letters, digits, '-' and '_'." };
  }

  let trigger: AutonomousTask["trigger"];
  if (form.triggerType === "cron") {
    if (!form.cronSchedule.trim()) return { error: "Cron schedule is required." };
    trigger = { type: "cron", schedule: form.cronSchedule.trim() };
  } else if (form.triggerType === "interval") {
    const parseField = (raw: string): number | null => {
      const v = raw.trim();
      if (!v) return null;
      const n = Number(v);
      if (!Number.isFinite(n) || n <= 0 || !Number.isInteger(n)) {
        return Number.NaN;
      }
      return n;
    };
    const seconds = parseField(form.intervalSeconds);
    const minutes = parseField(form.intervalMinutes);
    const hours = parseField(form.intervalHours);
    if ([seconds, minutes, hours].some((v) => Number.isNaN(v))) {
      return { error: "Interval values must be positive whole numbers." };
    }
    if (seconds == null && minutes == null && hours == null) {
      return { error: "Interval requires at least one of seconds / minutes / hours." };
    }
    trigger = {
      type: "interval",
      seconds: seconds ?? null,
      minutes: minutes ?? null,
      hours: hours ?? null,
    };
  } else {
    trigger = {
      type: "webhook",
      // Treat empty input as "no secret" rather than "empty secret"
      // — the latter would (correctly) be rejected by HMAC validation
      // downstream and is almost certainly a UI mistake.
      secret: form.webhookSecret.trim() ? form.webhookSecret.trim() : null,
    };
  }

  let timeoutSeconds: number | null = null;
  if (form.timeoutSeconds.trim()) {
    const n = Number(form.timeoutSeconds);
    if (!Number.isFinite(n) || n <= 0) {
      return { error: "Timeout must be a positive number of seconds." };
    }
    timeoutSeconds = n;
  }

  let maxRetries: number | null = null;
  if (form.maxRetries.trim()) {
    const n = Number(form.maxRetries);
    if (!Number.isFinite(n) || n < 0 || !Number.isInteger(n)) {
      return { error: "Max retries must be a non-negative integer." };
    }
    maxRetries = n;
  }

  const task: AutonomousTask = {
    id,
    name,
    description: form.description.trim() || null,
    // Empty agent => null on the wire (FR-001: agent is optional hint).
    agent: agent || null,
    prompt,
    llm_provider: form.llm_provider.trim() || null,
    trigger,
    enabled: form.enabled,
    timeout_seconds: timeoutSeconds,
    max_retries: maxRetries,
  };
  return { task };
}

export function TaskFormDialog({ open, onOpenChange, task, onSubmit }: TaskFormDialogProps) {
  const isEdit = Boolean(task);
  const [form, setForm] = useState<TaskFormState>(() => toFormState(task));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset whenever the dialog opens or the underlying task changes.
  // Without this, editing task A then opening "create" would inherit
  // A's fields.
  useEffect(() => {
    if (open) {
      setForm(toFormState(task));
      setError(null);
      setSubmitting(false);
    }
  }, [open, task]);

  const update = <K extends keyof TaskFormState>(key: K, value: TaskFormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const triggerOptions = useMemo<TriggerType[]>(() => ["cron", "interval", "webhook"], []);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    const result = fromFormState(form);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(result.task);
      onOpenChange(false);
    } catch (err) {
      // Mirror the API client's error shape — `.message` already
      // carries the FastAPI ``detail`` string when available.
      setError(err instanceof Error ? err.message : "Failed to save task.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit task" : "New autonomous task"}</DialogTitle>
          <DialogDescription>
            Tasks are scheduled via the autonomous-agents service and dispatched to
            CAIPE supervisor over A2A. Cron and interval tasks fire automatically;
            webhook tasks fire when a POST hits{" "}
            <code className="text-xs">/api/v1/hooks/{form.id || "<id>"}</code>.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="task-id">ID</Label>
              <Input
                id="task-id"
                value={form.id}
                onChange={(e) => update("id", e.target.value)}
                placeholder="daily-incident-summary"
                disabled={isEdit}
                required
              />
              {isEdit && (
                <p className="text-[11px] text-muted-foreground">
                  ID is immutable after creation.
                </p>
              )}
            </div>
            <div className="space-y-1">
              <Label htmlFor="task-name">Name</Label>
              <Input
                id="task-name"
                value={form.name}
                onChange={(e) => update("name", e.target.value)}
                placeholder="Daily Incident Summary"
                required
              />
            </div>
          </div>

          <div className="space-y-1">
            <Label htmlFor="task-description">Description</Label>
            <Input
              id="task-description"
              value={form.description}
              onChange={(e) => update("description", e.target.value)}
              placeholder="What does this task do?"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="task-agent">Agent (optional)</Label>
              <Input
                id="task-agent"
                value={form.agent}
                onChange={(e) => update("agent", e.target.value)}
                placeholder="leave blank to let the LLM router decide"
              />
              <p className="text-[11px] text-muted-foreground">
                Optional routing hint (e.g. <code>github</code>). Leave blank
                and the supervisor&apos;s LLM picks an agent from the prompt
                at run time.
              </p>
            </div>
            <div className="space-y-1">
              <Label htmlFor="task-llm">LLM provider (optional)</Label>
              <Input
                id="task-llm"
                value={form.llm_provider}
                onChange={(e) => update("llm_provider", e.target.value)}
                placeholder="anthropic"
              />
            </div>
          </div>

          <div className="space-y-1">
            <Label htmlFor="task-prompt">Prompt</Label>
            <Textarea
              id="task-prompt"
              value={form.prompt}
              onChange={(e) => update("prompt", e.target.value)}
              rows={4}
              placeholder="Summarise yesterday's incidents and post to #ops."
              required
            />
          </div>

          <div className="space-y-2 rounded-md border border-border p-3">
            <Label>Trigger</Label>
            <div className="flex gap-2">
              {triggerOptions.map((opt) => (
                <button
                  type="button"
                  key={opt}
                  onClick={() => update("triggerType", opt)}
                  className={`px-3 py-1 text-xs rounded-md border transition-colors ${
                    form.triggerType === opt
                      ? "bg-primary text-primary-foreground border-primary"
                      : "bg-background text-foreground border-border hover:bg-muted"
                  }`}
                >
                  {opt}
                </button>
              ))}
            </div>

            {form.triggerType === "cron" && (
              <div className="space-y-1">
                <Label htmlFor="task-cron">Schedule (cron)</Label>
                <Input
                  id="task-cron"
                  value={form.cronSchedule}
                  onChange={(e) => update("cronSchedule", e.target.value)}
                  placeholder="0 9 * * *"
                  required
                />
                <p className="text-[11px] text-muted-foreground">
                  Standard 5-field cron expression (minute hour dom month dow).
                </p>
              </div>
            )}

            {form.triggerType === "interval" && (
              <div className="grid grid-cols-3 gap-2">
                <div className="space-y-1">
                  <Label htmlFor="task-interval-seconds">Seconds</Label>
                  <Input
                    id="task-interval-seconds"
                    value={form.intervalSeconds}
                    onChange={(e) => update("intervalSeconds", e.target.value)}
                    inputMode="numeric"
                    placeholder=""
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="task-interval-minutes">Minutes</Label>
                  <Input
                    id="task-interval-minutes"
                    value={form.intervalMinutes}
                    onChange={(e) => update("intervalMinutes", e.target.value)}
                    inputMode="numeric"
                    placeholder=""
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="task-interval-hours">Hours</Label>
                  <Input
                    id="task-interval-hours"
                    value={form.intervalHours}
                    onChange={(e) => update("intervalHours", e.target.value)}
                    inputMode="numeric"
                    placeholder="1"
                  />
                </div>
              </div>
            )}

            {form.triggerType === "webhook" && (
              <div className="space-y-1">
                <Label htmlFor="task-webhook-secret">HMAC secret (optional)</Label>
                <Input
                  id="task-webhook-secret"
                  value={form.webhookSecret}
                  onChange={(e) => update("webhookSecret", e.target.value)}
                  type="password"
                  placeholder={
                    isEdit && task?.trigger.type === "webhook" && task.trigger.has_secret
                      ? "secret already configured — type to replace"
                      : "leave blank to accept unsigned payloads"
                  }
                />
                {isEdit && task?.trigger.type === "webhook" && task.trigger.has_secret && (
                  <p className="text-xs text-muted-foreground">
                    The existing secret is hidden for security. Leave this field blank to keep it unchanged.
                  </p>
                )}
              </div>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="task-timeout">Timeout (seconds, optional)</Label>
              <Input
                id="task-timeout"
                value={form.timeoutSeconds}
                onChange={(e) => update("timeoutSeconds", e.target.value)}
                inputMode="decimal"
                placeholder="defaults to A2A_TIMEOUT_SECONDS"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="task-retries">Max retries (optional)</Label>
              <Input
                id="task-retries"
                value={form.maxRetries}
                onChange={(e) => update("maxRetries", e.target.value)}
                inputMode="numeric"
                placeholder="defaults to A2A_MAX_RETRIES"
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => update("enabled", e.target.checked)}
              className="h-4 w-4 rounded border-border"
            />
            Enabled
          </label>

          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
              {error}
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting ? "Saving…" : isEdit ? "Save changes" : "Create task"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
