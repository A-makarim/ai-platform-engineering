// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Plus, RefreshCw, Bot } from "lucide-react";

import { AuthGuard } from "@/components/auth-guard";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";

import {
  autonomousApi,
  AutonomousApiError,
  type AutonomousTask,
} from "@/components/autonomous/api";
import { TaskList } from "@/components/autonomous/TaskList";
import { TaskFormDialog } from "@/components/autonomous/TaskFormDialog";
import { RunHistory } from "@/components/autonomous/RunHistory";

export default function AutonomousAgentsPage() {
  return (
    <AuthGuard>
      <AutonomousAgentsView />
    </AuthGuard>
  );
}

function AutonomousAgentsView() {
  const { toast } = useToast();
  const [tasks, setTasks] = useState<AutonomousTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingTask, setEditingTask] = useState<AutonomousTask | null>(null);
  const [runHistoryRefreshKey, setRunHistoryRefreshKey] = useState(0);

  const selectedTask = useMemo(
    () => tasks.find((t) => t.id === selectedId) ?? null,
    [tasks, selectedId],
  );

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const data = await autonomousApi.listTasks();
      setTasks(data);
      setLoadError(null);
      // Auto-select first task on initial load so the right pane is
      // never empty for an operator who already has tasks configured.
      setSelectedId((current) => {
        if (current && data.some((t) => t.id === current)) return current;
        return data[0]?.id ?? null;
      });
    } catch (err) {
      const msg =
        err instanceof AutonomousApiError
          ? err.message
          : "Failed to reach the autonomous-agents service. Is it running on :8002?";
      setLoadError(msg);
      setTasks([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const markBusy = (id: string, busy: boolean) => {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const handleCreate = () => {
    setEditingTask(null);
    setDialogOpen(true);
  };

  const handleEdit = (task: AutonomousTask) => {
    setEditingTask(task);
    setDialogOpen(true);
  };

  const handleSubmitTask = async (task: AutonomousTask) => {
    if (editingTask) {
      const updated = await autonomousApi.updateTask(editingTask.id, task);
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      setSelectedId(updated.id);
      toast(`Task "${updated.name}" updated.`, "success");
    } else {
      const created = await autonomousApi.createTask(task);
      setTasks((prev) => [...prev, created]);
      setSelectedId(created.id);
      toast(`Task "${created.name}" created.`, "success");
    }
  };

  const handleDelete = async (task: AutonomousTask) => {
    // ``window.confirm`` is intentional — the autonomous tab is an
    // operator surface and a custom modal would be overkill. If a
    // future PR introduces a project-wide confirm dialog component,
    // swap this for it.
    if (!window.confirm(`Delete task "${task.name}"? This cannot be undone.`)) {
      return;
    }
    markBusy(task.id, true);
    try {
      await autonomousApi.deleteTask(task.id);
      setTasks((prev) => prev.filter((t) => t.id !== task.id));
      if (selectedId === task.id) setSelectedId(null);
      toast(`Task "${task.name}" deleted.`, "success");
    } catch (err) {
      const msg = err instanceof AutonomousApiError ? err.message : "Failed to delete task";
      toast(msg, "error");
    } finally {
      markBusy(task.id, false);
    }
  };

  const handleTrigger = async (task: AutonomousTask) => {
    markBusy(task.id, true);
    try {
      await autonomousApi.triggerTask(task.id);
      toast(`Triggered "${task.name}". Run history will update shortly.`, "success");
      // Bump the refresh key so the right-pane history reloads even
      // if the user has it focused.
      setRunHistoryRefreshKey((n) => n + 1);
      // Surface the new ``next_run`` value on the card.
      try {
        const refreshed = await autonomousApi.getTask(task.id);
        setTasks((prev) => prev.map((t) => (t.id === refreshed.id ? refreshed : t)));
      } catch {
        // Non-fatal; the next full reload will catch it.
      }
    } catch (err) {
      const msg = err instanceof AutonomousApiError ? err.message : "Failed to trigger task";
      toast(msg, "error");
    } finally {
      markBusy(task.id, false);
    }
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <header className="shrink-0 border-b border-border px-6 py-4 flex items-center gap-3">
        <div className="flex items-center gap-2">
          <Bot className="h-5 w-5 text-primary" />
          <h1 className="text-lg font-semibold">Autonomous Agents</h1>
        </div>
        <span className="text-xs text-muted-foreground hidden sm:inline">
          Schedule and trigger CAIPE tasks without a human in the loop.
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={reload}
            disabled={loading}
          >
            <RefreshCw className={cn("h-3.5 w-3.5 mr-1.5", loading && "animate-spin")} />
            Refresh
          </Button>
          <Button type="button" size="sm" onClick={handleCreate}>
            <Plus className="h-3.5 w-3.5 mr-1.5" />
            New task
          </Button>
        </div>
      </header>

      {loadError && (
        <div className="mx-6 mt-4 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-300">
          {loadError}
        </div>
      )}

      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-4 px-6 py-4 overflow-hidden">
        <section className="overflow-y-auto">
          {loading && tasks.length === 0 ? (
            <div className="rounded-md border border-border px-4 py-12 text-center text-sm text-muted-foreground">
              Loading tasks…
            </div>
          ) : (
            <TaskList
              tasks={tasks}
              selectedTaskId={selectedId}
              onSelect={(t) => setSelectedId(t.id)}
              onEdit={handleEdit}
              onDelete={handleDelete}
              onTrigger={handleTrigger}
              busyIds={busyIds}
            />
          )}
        </section>

        <section className="overflow-y-auto rounded-lg border border-border bg-card p-4">
          {selectedTask ? (
            <div className="space-y-4">
              <div>
                <h2 className="text-base font-semibold text-foreground">
                  {selectedTask.name}
                </h2>
                {selectedTask.description && (
                  <p className="text-xs text-muted-foreground mt-1">
                    {selectedTask.description}
                  </p>
                )}
              </div>
              <dl className="grid grid-cols-2 gap-y-1 text-xs">
                <dt className="text-muted-foreground">Agent</dt>
                <dd className="font-mono text-foreground">{selectedTask.agent}</dd>
                <dt className="text-muted-foreground">Trigger</dt>
                <dd className="font-mono text-foreground">{selectedTask.trigger.type}</dd>
                {selectedTask.timeout_seconds != null && (
                  <>
                    <dt className="text-muted-foreground">Timeout</dt>
                    <dd className="font-mono text-foreground">
                      {selectedTask.timeout_seconds}s
                    </dd>
                  </>
                )}
                {selectedTask.max_retries != null && (
                  <>
                    <dt className="text-muted-foreground">Max retries</dt>
                    <dd className="font-mono text-foreground">{selectedTask.max_retries}</dd>
                  </>
                )}
              </dl>
              <div>
                <div className="text-xs font-medium text-muted-foreground mb-1">Prompt</div>
                <pre className="whitespace-pre-wrap break-words text-xs rounded bg-muted p-3 text-foreground">
                  {selectedTask.prompt}
                </pre>
              </div>
              <RunHistory
                taskId={selectedTask.id}
                refreshKey={runHistoryRefreshKey}
              />
            </div>
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              {tasks.length === 0
                ? "Create a task to get started."
                : "Select a task to view details and run history."}
            </div>
          )}
        </section>
      </div>

      <TaskFormDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        task={editingTask}
        onSubmit={handleSubmitTask}
      />
    </div>
  );
}
