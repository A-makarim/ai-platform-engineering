// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

"use client";

import React from "react";
import { Pencil, Play, Trash2, Webhook, Clock, Repeat } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { AutonomousTask, Trigger } from "./types";

interface TaskListProps {
  tasks: AutonomousTask[];
  selectedTaskId: string | null;
  onSelect: (task: AutonomousTask) => void;
  onEdit: (task: AutonomousTask) => void;
  onDelete: (task: AutonomousTask) => void;
  onTrigger: (task: AutonomousTask) => void;
  /** ids that are currently being acted on (delete/trigger) — used to grey out buttons. */
  busyIds: Set<string>;
}

function describeTrigger(trigger: Trigger): string {
  if (trigger.type === "cron") return `cron · ${trigger.schedule}`;
  if (trigger.type === "interval") {
    const parts: string[] = [];
    if (trigger.hours) parts.push(`${trigger.hours}h`);
    if (trigger.minutes) parts.push(`${trigger.minutes}m`);
    if (trigger.seconds) parts.push(`${trigger.seconds}s`);
    return `every ${parts.join(" ") || "—"}`;
  }
  return "webhook";
}

function TriggerIcon({ type }: { type: Trigger["type"] }) {
  if (type === "cron") return <Clock className="h-3.5 w-3.5" />;
  if (type === "interval") return <Repeat className="h-3.5 w-3.5" />;
  return <Webhook className="h-3.5 w-3.5" />;
}

function formatNextRun(value?: string | null): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

export function TaskList({
  tasks,
  selectedTaskId,
  onSelect,
  onEdit,
  onDelete,
  onTrigger,
  busyIds,
}: TaskListProps) {
  if (tasks.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border px-4 py-12 text-center text-sm text-muted-foreground">
        No autonomous tasks yet. Click &quot;New task&quot; to create one.
      </div>
    );
  }

  return (
    <ul className="flex flex-col gap-2">
      {tasks.map((task) => {
        const isSelected = task.id === selectedTaskId;
        const isBusy = busyIds.has(task.id);
        return (
          <li key={task.id}>
            <div
              className={cn(
                "rounded-lg border bg-card text-card-foreground transition-colors",
                isSelected ? "border-primary ring-1 ring-primary/30" : "border-border hover:border-primary/40",
                !task.enabled && "opacity-60",
              )}
            >
              <button
                type="button"
                onClick={() => onSelect(task)}
                className="w-full text-left px-4 py-3"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-semibold truncate text-foreground">
                        {task.name}
                      </h3>
                      <Badge variant="outline" className="shrink-0 font-mono text-[10px]">
                        {task.id}
                      </Badge>
                      {!task.enabled && (
                        <Badge variant="secondary" className="shrink-0 text-[10px] uppercase">
                          disabled
                        </Badge>
                      )}
                    </div>
                    {task.description && (
                      <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
                        {task.description}
                      </p>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                      <span className="inline-flex items-center gap-1">
                        <TriggerIcon type={task.trigger.type} />
                        {describeTrigger(task.trigger)}
                      </span>
                      <span>agent: <code className="text-[11px]">{task.agent}</code></span>
                      <span>next: {formatNextRun(task.next_run)}</span>
                    </div>
                  </div>
                </div>
              </button>
              <div className="flex items-center justify-end gap-1 border-t border-border px-2 py-1.5">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    onTrigger(task);
                  }}
                  disabled={isBusy || !task.enabled}
                  title={task.enabled ? "Run now" : "Enable the task to run it"}
                >
                  <Play className="h-3.5 w-3.5" />
                  <span className="ml-1 text-xs">Run</span>
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    onEdit(task);
                  }}
                  disabled={isBusy}
                >
                  <Pencil className="h-3.5 w-3.5" />
                  <span className="ml-1 text-xs">Edit</span>
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(task);
                  }}
                  disabled={isBusy}
                  className="text-red-600 hover:text-red-700 hover:bg-red-500/10"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  <span className="ml-1 text-xs">Delete</span>
                </Button>
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
