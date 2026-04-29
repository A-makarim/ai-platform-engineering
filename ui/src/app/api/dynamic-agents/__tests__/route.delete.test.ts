/**
 * @jest-environment node
 */
// Copyright CNOE Contributors (https://cnoe.io)
// SPDX-License-Identifier: Apache-2.0

/**
 * Tests for the DELETE /api/dynamic-agents cascade flow.
 *
 * The DELETE handler must call POST
 * ``/tasks/disable-by-dynamic-agent/<id>`` on the autonomous-agents
 * service BEFORE removing the agent doc from MongoDB. If the cascade
 * fails (network error or non-2xx upstream), the agent doc must NOT
 * be deleted -- otherwise the operator ends up with autonomous tasks
 * pointing at a vanished agent, firing on every tick into the same
 * "ack failed always" failure mode this workflow was built to fix.
 *
 * Tests:
 *   1. Successful cascade -> deleteOne is called, fetch hits the
 *      cascade URL with POST, response is 200.
 *   2. Cascade returns non-2xx -> deleteOne is NOT called, response
 *      is 502 with a clear error message.
 *   3. Cascade fetch throws -> deleteOne is NOT called, response is
 *      502 wrapping the network error.
 */

import { NextRequest } from 'next/server';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

jest.mock('next-auth', () => ({ getServerSession: jest.fn() }));
const mockGetServerSession = jest.requireMock<{ getServerSession: jest.Mock }>(
  'next-auth',
).getServerSession;

jest.mock('@/lib/auth-config', () => ({ authOptions: {} }));

// MongoDB collection mock. We model just the methods the DELETE handler
// touches (``findOne``, ``deleteOne``) and let getCollection return our
// stub so we can assert deleteOne was/wasn't called per scenario.
const mockFindOne = jest.fn();
const mockDeleteOne = jest.fn();

jest.mock('@/lib/mongodb', () => ({
  getCollection: jest.fn().mockResolvedValue({
    findOne: (...args: unknown[]) => mockFindOne(...args),
    deleteOne: (...args: unknown[]) => mockDeleteOne(...args),
  }),
}));

jest.mock('@/lib/config', () => ({
  getConfig: (key: string) => (key === 'ssoEnabled' ? true : undefined),
}));

// Pin the cascade target so the URL assertion is deterministic
// regardless of which env vars happen to be set on the runner.
process.env.AUTONOMOUS_AGENTS_URL = 'http://test-autonomous:8002';

const mockFetch = jest.fn();
beforeAll(() => {
  (globalThis as { fetch: unknown }).fetch = mockFetch;
});

jest.spyOn(console, 'error').mockImplementation(() => {});
jest.spyOn(console, 'warn').mockImplementation(() => {});

import { DELETE } from '../route';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function adminSession() {
  return {
    user: { email: 'admin@example.com', name: 'Admin' },
    role: 'admin',
    canViewAdmin: true,
  };
}

function deleteRequest(id: string): NextRequest {
  const url = new URL(
    `/api/dynamic-agents?id=${encodeURIComponent(id)}`,
    'http://localhost:3000',
  );
  return new NextRequest(url, { method: 'DELETE' });
}

function okJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

function errorResponse(status: number, body: string) {
  return new Response(body, {
    status,
    headers: { 'content-type': 'text/plain' },
  });
}

beforeEach(() => {
  jest.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Cascade success
// ---------------------------------------------------------------------------

describe('DELETE /api/dynamic-agents — cascade-then-delete flow', () => {
  it('cascades to autonomous-agents and deletes the agent on success', async () => {
    mockGetServerSession.mockResolvedValue(adminSession());
    mockFindOne.mockResolvedValue({
      _id: 'my-agent',
      is_system: false,
      config_driven: false,
    });
    mockFetch.mockResolvedValue(
      okJsonResponse({ disabled_count: 2, task_ids: ['t1', 't2'] }),
    );
    mockDeleteOne.mockResolvedValue({ deletedCount: 1 });

    const res = await DELETE(deleteRequest('my-agent'));

    expect(res.status).toBe(200);

    // Cascade hit before delete.
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, options] = mockFetch.mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe(
      'http://test-autonomous:8002/api/v1/tasks/disable-by-dynamic-agent/my-agent',
    );
    expect(options.method).toBe('POST');

    // Then delete.
    expect(mockDeleteOne).toHaveBeenCalledTimes(1);
    expect(mockDeleteOne).toHaveBeenCalledWith({ _id: 'my-agent' });
  });

  it('URL-encodes the agent id when building the cascade URL', async () => {
    // The id flows from a query string into a URL path segment, so a
    // pathological id with a slash or # must not silently change the
    // upstream route. encodeURIComponent in the handler defends here.
    mockGetServerSession.mockResolvedValue(adminSession());
    mockFindOne.mockResolvedValue({
      _id: 'weird/id#1',
      is_system: false,
      config_driven: false,
    });
    mockFetch.mockResolvedValue(okJsonResponse({ disabled_count: 0 }));
    mockDeleteOne.mockResolvedValue({ deletedCount: 1 });

    await DELETE(deleteRequest('weird/id#1'));

    const [url] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      'http://test-autonomous:8002/api/v1/tasks/disable-by-dynamic-agent/weird%2Fid%231',
    );
  });

  // -------------------------------------------------------------------------
  // Cascade failure modes -- agent must remain intact.
  // -------------------------------------------------------------------------

  it('returns 502 and does NOT delete when the cascade returns non-2xx', async () => {
    mockGetServerSession.mockResolvedValue(adminSession());
    mockFindOne.mockResolvedValue({
      _id: 'my-agent',
      is_system: false,
      config_driven: false,
    });
    mockFetch.mockResolvedValue(errorResponse(500, 'cascade exploded'));

    const res = await DELETE(deleteRequest('my-agent'));

    expect(res.status).toBe(502);
    const body = (await res.json()) as { error?: string };
    expect(body.error).toMatch(/Failed to disable linked autonomous tasks/);
    expect(body.error).toMatch(/Agent was NOT/);
    // Critical: the agent doc MUST stay alive so the next retry can
    // cascade cleanly. A regression here would re-introduce zombie
    // tasks against a missing agent.
    expect(mockDeleteOne).not.toHaveBeenCalled();
  });

  it('returns 502 and does NOT delete when the cascade fetch throws', async () => {
    mockGetServerSession.mockResolvedValue(adminSession());
    mockFindOne.mockResolvedValue({
      _id: 'my-agent',
      is_system: false,
      config_driven: false,
    });
    mockFetch.mockRejectedValue(new Error('ECONNREFUSED'));

    const res = await DELETE(deleteRequest('my-agent'));

    expect(res.status).toBe(502);
    const body = (await res.json()) as { error?: string };
    expect(body.error).toMatch(
      /Failed to reach autonomous-agents service/,
    );
    expect(body.error).toMatch(/ECONNREFUSED/);
    expect(mockDeleteOne).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------
  // Pre-cascade guards still fire so the cascade isn't called for
  // ineligible agents (system / config-driven).
  // -------------------------------------------------------------------------

  it('skips the cascade when the agent is not found', async () => {
    mockGetServerSession.mockResolvedValue(adminSession());
    mockFindOne.mockResolvedValue(null);

    const res = await DELETE(deleteRequest('ghost'));

    expect(res.status).toBe(404);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(mockDeleteOne).not.toHaveBeenCalled();
  });

  it('skips the cascade for system agents', async () => {
    mockGetServerSession.mockResolvedValue(adminSession());
    mockFindOne.mockResolvedValue({
      _id: 'sys',
      is_system: true,
      config_driven: false,
    });

    const res = await DELETE(deleteRequest('sys'));

    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(mockDeleteOne).not.toHaveBeenCalled();
  });

  it('skips the cascade for config-driven agents', async () => {
    mockGetServerSession.mockResolvedValue(adminSession());
    mockFindOne.mockResolvedValue({
      _id: 'cfg',
      is_system: false,
      config_driven: true,
    });

    const res = await DELETE(deleteRequest('cfg'));

    expect(res.status).toBe(403);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(mockDeleteOne).not.toHaveBeenCalled();
  });
});
