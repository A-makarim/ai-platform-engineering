// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

import { NextRequest, NextResponse } from 'next/server';
import { getServerSession } from 'next-auth';

import { authOptions } from '@/lib/auth-config';

/**
 * Autonomous Agents API Proxy.
 *
 * Forwards every method (GET/POST/PUT/PATCH/DELETE) under
 * `/api/autonomous/<...path>` to the autonomous-agents FastAPI
 * service at `AUTONOMOUS_AGENTS_URL` (default `http://localhost:8002`).
 *
 * Why a proxy instead of calling the FastAPI service directly from the
 * browser:
 *   1. The autonomous-agents service binds to 8002 and isn't exposed
 *      publicly in any deployment topology -- the UI is the only
 *      sanctioned entry point.
 *   2. Auth lives at the Next.js boundary. We require a NextAuth
 *      session here and (eventually) forward the JWT downstream so
 *      the autonomous-agents service can pick up the same identity
 *      semantics as the RAG proxy. Today the service is
 *      localhost-only so we skip the Bearer step until the
 *      service-side auth (IMP-10) is shipped.
 *   3. Centralising the URL keeps the React side decoupled -- code
 *      always hits `/api/autonomous/...` regardless of where the
 *      backend physically runs.
 *
 * Auth gating: a missing/expired session returns 401. We don't try to
 * be clever about anonymous access here -- the autonomous task
 * surface is only meaningful for signed-in operators.
 */

/**
 * Base URL for the autonomous-agents service, **without** the API
 * version prefix. Operators set this to e.g.
 * ``http://localhost:8002`` or ``http://autonomous-agents:8002``;
 * the ``/api/v1`` segment is added by ``buildTargetUrl`` below so a
 * prefix bump (v2 etc.) only touches one constant.
 */
function getAutonomousAgentsUrl(): string {
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
const AUTONOMOUS_API_PREFIX = '/api/v1';

async function requireSession(): Promise<{ ok: true } | { ok: false; response: NextResponse }> {
  try {
    const session = await getServerSession(authOptions);
    // Falsey session OR a session without an authenticated user counts
    // as unauthorised. Mirrors the pattern used by the chat proxies.
    if (!session?.user) {
      return {
        ok: false,
        response: NextResponse.json(
          { error: 'Authentication required' },
          { status: 401 },
        ),
      };
    }
    return { ok: true };
  } catch (error) {
    console.error('[Autonomous Proxy] Session lookup failed:', error);
    return {
      ok: false,
      response: NextResponse.json(
        { error: 'Authentication unavailable' },
        { status: 503 },
      ),
    };
  }
}

function buildTargetUrl(request: NextRequest, pathSegments: string[]): URL {
  const targetPath = pathSegments.join('/');
  // Strip a leading slash on the env var so we don't end up with
  // ``//api/v1/tasks`` (some HTTP stacks tolerate it, others don't).
  const base = getAutonomousAgentsUrl().replace(/\/$/, '');
  const targetUrl = new URL(`${base}${AUTONOMOUS_API_PREFIX}/${targetPath}`);
  // Forward query parameters (the run-history endpoints don't take any
  // today, but future pagination params would otherwise be silently
  // dropped here).
  request.nextUrl.searchParams.forEach((value, key) => {
    targetUrl.searchParams.append(key, value);
  });
  return targetUrl;
}

async function readBody(request: NextRequest): Promise<unknown> {
  const contentLength = request.headers.get('content-length');
  if (!contentLength || parseInt(contentLength, 10) === 0) {
    return undefined;
  }
  try {
    return await request.json();
  } catch {
    // Some endpoints (manual trigger) accept an empty body; others
    // would reject malformed JSON downstream. Either way, surfacing
    // ``undefined`` here is correct -- we only forward a body if we
    // managed to parse one.
    return undefined;
  }
}

async function forward(
  request: NextRequest,
  pathSegments: string[],
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',
): Promise<NextResponse> {
  const guard = await requireSession();
  if (guard.ok === false) return guard.response;

  const targetUrl = buildTargetUrl(request, pathSegments);
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };

  const fetchOptions: RequestInit = { method, headers };
  if (method !== 'GET' && method !== 'DELETE') {
    const body = await readBody(request);
    if (body !== undefined) {
      fetchOptions.body = JSON.stringify(body);
    }
  }

  try {
    const response = await fetch(targetUrl.toString(), fetchOptions);

    // 204 No Content -- pass through verbatim so the UI sees a clean
    // success without a body parse attempt.
    if (response.status === 204) {
      return new NextResponse(null, { status: 204 });
    }

    // The autonomous-agents service always replies in JSON, but read
    // as text first so we can surface a useful error envelope even if
    // the upstream returned a non-JSON body (e.g. on a crash).
    const text = await response.text();
    if (!text) {
      return new NextResponse(null, { status: response.status });
    }
    try {
      const data = JSON.parse(text);
      return NextResponse.json(data, { status: response.status });
    } catch {
      return NextResponse.json(
        { error: 'Upstream returned non-JSON response', body: text.slice(0, 500) },
        { status: response.status },
      );
    }
  } catch (error) {
    console.error(`[Autonomous Proxy] ${method} ${targetUrl} failed:`, error);
    return NextResponse.json(
      { error: 'Failed to reach autonomous-agents service', details: String(error) },
      { status: 502 },
    );
  }
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path, 'GET');
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path, 'POST');
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path, 'PUT');
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path, 'PATCH');
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path, 'DELETE');
}
