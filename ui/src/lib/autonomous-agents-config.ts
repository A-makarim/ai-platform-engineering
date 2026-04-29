// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

/**
 * Shared configuration for talking to the autonomous-agents FastAPI
 * service from Next.js server code.
 *
 * Two callers depend on this today:
 *
 *   1. The autonomous proxy (`ui/src/app/api/autonomous/[...path]`),
 *      which forwards every UI-side autonomous tasks/runs request.
 *   2. The dynamic-agents DELETE handler
 *      (`ui/src/app/api/dynamic-agents/route.ts`), which calls the
 *      cascade-disable endpoint **before** removing the agent doc so
 *      tasks pointing at the soon-to-be-gone agent stop firing.
 *
 * Centralising the URL + prefix here means a deployment topology
 * change (e.g. swapping the in-cluster service host or bumping the
 * API version) is a one-line edit instead of a spelunk through every
 * caller.
 */

/**
 * Base URL for the autonomous-agents service, **without** the API
 * version prefix. Operators set this to e.g.
 * ``http://localhost:8002`` or ``http://autonomous-agents:8002``;
 * the ``/api/v1`` segment is added by callers via
 * ``AUTONOMOUS_API_PREFIX`` so a prefix bump (v2 etc.) only touches
 * one constant.
 *
 * NEXT_PUBLIC_ fallback is kept for parity with other lib helpers
 * even though this module is server-only -- some legacy deployments
 * only set the public flavour.
 */
export function getAutonomousAgentsUrl(): string {
  return (
    process.env.AUTONOMOUS_AGENTS_URL ||
    process.env.NEXT_PUBLIC_AUTONOMOUS_AGENTS_URL ||
    'http://localhost:8002'
  );
}

/**
 * FastAPI mounts both the tasks and webhooks routers under
 * ``/api/v1`` (see ``autonomous_agents/main.py``). We hard-code the
 * prefix here rather than baking it into the env var so:
 *   1. operators can copy/paste the same URL they'd use for a
 *      ``curl localhost:8002/healthz`` smoke test, AND
 *   2. an upstream version bump (v1 -> v2) is a one-line edit.
 */
export const AUTONOMOUS_API_PREFIX = '/api/v1';
