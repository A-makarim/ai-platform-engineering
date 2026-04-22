// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

import type { AutonomousTask } from "@/components/autonomous/types";
import {
  syncAutonomousTasks,
  type AutonomousTasksApi,
} from "../syncAutonomousTasks";

function cronTask(id: string, overrides: Partial<AutonomousTask> = {}): AutonomousTask {
  return {
    id,
    name: `Task ${id}`,
    description: null,
    agent: "my_agent",
    prompt: "hello",
    llm_provider: null,
    trigger: { type: "cron", schedule: "0 9 * * *" },
    enabled: true,
    timeout_seconds: null,
    max_retries: null,
    ...overrides,
  };
}

function makeApi(): jest.Mocked<AutonomousTasksApi> {
  return {
    createTask: jest.fn().mockImplementation(async (t: AutonomousTask) => t),
    updateTask: jest.fn().mockImplementation(async (_id: string, t: AutonomousTask) => t),
    deleteTask: jest.fn().mockResolvedValue(undefined),
  };
}

describe("syncAutonomousTasks", () => {
  it("stamps agent id onto created tasks", async () => {
    const api = makeApi();
    const draft = cronTask("t1", { agent: null });

    const results = await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [draft],
      serverTasks: [],
      api,
    });

    expect(api.createTask).toHaveBeenCalledTimes(1);
    const submitted = api.createTask.mock.calls[0][0];
    expect(submitted.agent).toBe("my_agent");
    expect(results).toEqual([{ op: "create", taskId: "t1", ok: true }]);
  });

  it("updates tasks whose editable fields changed", async () => {
    const api = makeApi();
    const server = cronTask("t1", { prompt: "old" });
    const draft = cronTask("t1", { prompt: "new" });

    const results = await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [draft],
      serverTasks: [server],
      api,
    });

    expect(api.updateTask).toHaveBeenCalledTimes(1);
    expect(api.updateTask).toHaveBeenCalledWith(
      "t1",
      expect.objectContaining({ prompt: "new", agent: "my_agent" }),
    );
    expect(results).toEqual([{ op: "update", taskId: "t1", ok: true }]);
  });

  it("skips updates when nothing changed", async () => {
    const api = makeApi();
    const task = cronTask("t1");

    const results = await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [task],
      serverTasks: [task],
      api,
    });

    expect(api.createTask).not.toHaveBeenCalled();
    expect(api.updateTask).not.toHaveBeenCalled();
    expect(api.deleteTask).not.toHaveBeenCalled();
    expect(results).toEqual([]);
  });

  it("deletes server tasks not present in drafts", async () => {
    const api = makeApi();
    const server1 = cronTask("keep");
    const server2 = cronTask("drop");

    const results = await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [server1],
      serverTasks: [server1, server2],
      api,
    });

    expect(api.deleteTask).toHaveBeenCalledWith("drop");
    expect(api.deleteTask).toHaveBeenCalledTimes(1);
    expect(results).toEqual([{ op: "delete", taskId: "drop", ok: true }]);
  });

  it("handles mixed create/update/delete in one pass", async () => {
    const api = makeApi();
    const existingUnchanged = cronTask("unchanged");
    const existingChanged = cronTask("changed", { enabled: true });
    const draftChanged = cronTask("changed", { enabled: false });
    const newDraft = cronTask("brand_new");
    const droppedServer = cronTask("dropped");

    const results = await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [existingUnchanged, draftChanged, newDraft],
      serverTasks: [existingUnchanged, existingChanged, droppedServer],
      api,
    });

    expect(api.createTask).toHaveBeenCalledWith(
      expect.objectContaining({ id: "brand_new" }),
    );
    expect(api.updateTask).toHaveBeenCalledWith(
      "changed",
      expect.objectContaining({ enabled: false }),
    );
    expect(api.deleteTask).toHaveBeenCalledWith("dropped");

    expect(results).toEqual(
      expect.arrayContaining([
        { op: "create", taskId: "brand_new", ok: true },
        { op: "update", taskId: "changed", ok: true },
        { op: "delete", taskId: "dropped", ok: true },
      ]),
    );
    expect(results).toHaveLength(3);
  });

  it("captures per-operation failures without throwing", async () => {
    const api = makeApi();
    api.createTask.mockRejectedValueOnce(new Error("boom"));

    const results = await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [cronTask("bad")],
      serverTasks: [],
      api,
    });

    expect(results).toEqual([
      { op: "create", taskId: "bad", ok: false, error: "boom" },
    ]);
  });

  it("treats webhook drafts with blank secret as equal to existing has_secret task", async () => {
    const api = makeApi();
    const serverWebhook: AutonomousTask = {
      ...cronTask("hook"),
      trigger: { type: "webhook", has_secret: true },
    };
    const draftWebhook: AutonomousTask = {
      ...cronTask("hook"),
      trigger: { type: "webhook", secret: null },
    };

    await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [draftWebhook],
      serverTasks: [serverWebhook],
      api,
    });

    expect(api.updateTask).not.toHaveBeenCalled();
  });

  it("updates webhook task when a new secret is typed", async () => {
    const api = makeApi();
    const serverWebhook: AutonomousTask = {
      ...cronTask("hook"),
      trigger: { type: "webhook", has_secret: false },
    };
    const draftWebhook: AutonomousTask = {
      ...cronTask("hook"),
      trigger: { type: "webhook", secret: "rotate-me" },
    };

    await syncAutonomousTasks({
      agentId: "my_agent",
      drafts: [draftWebhook],
      serverTasks: [serverWebhook],
      api,
    });

    expect(api.updateTask).toHaveBeenCalledTimes(1);
  });
});
