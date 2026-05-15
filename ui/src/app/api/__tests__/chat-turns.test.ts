/**
 * @jest-environment node
 */
/**
 * Tests for POST /api/chat/conversations/[id]/turns guard rails.
 *
 * The turns route is mostly read-only tooling (loadTurnsFromServer aliases
 * to loadMessagesFromServer); the explicit reject for autonomous_comment
 * matters because it pins the contract that follow-up writes flow only
 * through POST /messages, never through POST /turns.
 */

import { NextRequest } from 'next/server';
import { ObjectId } from 'mongodb';

const mockGetServerSession = jest.fn();
jest.mock('next-auth', () => ({
  getServerSession: (...args: unknown[]) => mockGetServerSession(...args),
}));
jest.mock('@/lib/auth-config', () => ({ authOptions: {} }));
jest.mock('@/lib/config', () => ({
  getConfig: (key: string) => key === 'ssoEnabled',
}));

const mockCollections: Record<string, ReturnType<typeof createMockCollection>> = {};
const mockGetCollection = jest.fn((name: string) => {
  if (!mockCollections[name]) {
    mockCollections[name] = createMockCollection();
  }
  return Promise.resolve(mockCollections[name]);
});
jest.mock('@/lib/mongodb', () => ({
  getCollection: (...args: unknown[]) => mockGetCollection(...args),
  isMongoDBConfigured: true,
}));

jest.spyOn(console, 'error').mockImplementation(() => {});
jest.spyOn(console, 'log').mockImplementation(() => {});
jest.spyOn(console, 'warn').mockImplementation(() => {});

function createMockCollection() {
  return {
    find: jest.fn().mockReturnValue({
      sort: jest.fn().mockReturnValue({
        skip: jest.fn().mockReturnValue({
          limit: jest.fn().mockReturnValue({
            toArray: jest.fn().mockResolvedValue([]),
          }),
        }),
      }),
      project: jest.fn().mockReturnValue({
        toArray: jest.fn().mockResolvedValue([]),
      }),
    }),
    findOne: jest.fn().mockResolvedValue(null),
    updateOne: jest.fn().mockResolvedValue({
      upsertedId: new ObjectId(),
      upsertedCount: 1,
      matchedCount: 0,
      modifiedCount: 0,
      acknowledged: true,
    }),
    countDocuments: jest.fn().mockResolvedValue(0),
  };
}

const TEST_CONV_ID = '11111111-1111-4111-8111-111111111111';

function makeRequest(url: string, options: RequestInit = {}): NextRequest {
  return new NextRequest(new URL(url, 'http://localhost:3000'), options);
}

import { POST } from '../chat/conversations/[id]/turns/route';

beforeEach(() => {
  jest.clearAllMocks();
  Object.keys(mockCollections).forEach((k) => delete mockCollections[k]);
});

describe('POST /api/chat/conversations/[id]/turns — read-only access guards', () => {
  function setupAutonomousConversation() {
    const convCol = createMockCollection();
    convCol.findOne.mockResolvedValue({
      _id: TEST_CONV_ID,
      owner_id: 'autonomous@system',
      source: 'autonomous',
      title: 'Autonomous task',
      sharing: { shared_with: [], shared_with_teams: [] },
    });
    mockCollections['conversations'] = convCol;

    const sharingCol = createMockCollection();
    sharingCol.findOne.mockResolvedValue(null);
    mockCollections['sharing_access'] = sharingCol;

    const usersCol = createMockCollection();
    usersCol.findOne.mockResolvedValue(null);
    mockCollections['users'] = usersCol;

    return convCol;
  }

  function setupNonAutonomousConversation(ownerEmail: string) {
    const convCol = createMockCollection();
    convCol.findOne.mockResolvedValue({
      _id: TEST_CONV_ID,
      owner_id: ownerEmail,
      title: 'Web conversation',
      sharing: { shared_with: [], shared_with_teams: [] },
    });
    mockCollections['conversations'] = convCol;

    const sharingCol = createMockCollection();
    sharingCol.findOne.mockResolvedValue(null);
    mockCollections['sharing_access'] = sharingCol;

    const usersCol = createMockCollection();
    usersCol.findOne.mockResolvedValue(null);
    mockCollections['users'] = usersCol;

    return convCol;
  }

  function postTurn(body: Record<string, unknown>) {
    const req = makeRequest(`/api/chat/conversations/${TEST_CONV_ID}/turns`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    return POST(req, { params: Promise.resolve({ id: TEST_CONV_ID }) });
  }

  it('rejects with 403 when admin session has autonomous_comment access', async () => {
    mockGetServerSession.mockResolvedValue({
      user: { email: 'admin@example.com', name: 'Admin' },
      role: 'admin',
      canViewAdmin: true,
    });
    setupAutonomousConversation();
    const turnsCol = createMockCollection();
    mockCollections['turns'] = turnsCol;

    const res = await postTurn({
      turn_id: 'turn-foo',
      client_type: 'ui',
      payload: { something: true },
    });

    expect(res.status).toBe(403);
    const body = await res.json();
    expect(body.code).toBe('FORBIDDEN');
    // Critical: must NOT have written anything to the turns collection.
    expect(turnsCol.updateOne).not.toHaveBeenCalled();
  });

  it('rejects with 403 when admin-view session has autonomous_comment access', async () => {
    mockGetServerSession.mockResolvedValue({
      user: { email: 'auditor@example.com', name: 'Auditor' },
      role: 'user',
      canViewAdmin: true,
    });
    setupAutonomousConversation();

    const res = await postTurn({
      turn_id: 'turn-foo',
      client_type: 'ui',
      payload: {},
    });

    expect(res.status).toBe(403);
    const body = await res.json();
    expect(body.code).toBe('FORBIDDEN');
  });

  it('rejects with 403 when admin session has admin_audit access (regression)', async () => {
    mockGetServerSession.mockResolvedValue({
      user: { email: 'admin@example.com', name: 'Admin' },
      role: 'admin',
      canViewAdmin: true,
    });
    setupNonAutonomousConversation('owner@example.com');

    const res = await postTurn({
      turn_id: 'turn-foo',
      client_type: 'ui',
      payload: {},
    });

    expect(res.status).toBe(403);
    const body = await res.json();
    expect(body.code).toBe('FORBIDDEN');
  });

  it('allows the conversation owner to upsert a turn', async () => {
    mockGetServerSession.mockResolvedValue({
      user: { email: 'owner@example.com', name: 'Owner' },
      role: 'user',
    });
    setupNonAutonomousConversation('owner@example.com');

    const turnsCol = createMockCollection();
    turnsCol.updateOne.mockResolvedValue({
      upsertedId: new ObjectId(),
      upsertedCount: 1,
      matchedCount: 0,
      modifiedCount: 0,
      acknowledged: true,
    });
    turnsCol.findOne.mockResolvedValue({
      _id: new ObjectId(),
      conversation_id: TEST_CONV_ID,
      client_type: 'ui',
      turn_id: 'turn-foo',
      payload: { ok: true },
    });
    mockCollections['turns'] = turnsCol;

    const res = await postTurn({
      turn_id: 'turn-foo',
      client_type: 'ui',
      payload: { ok: true },
    });

    expect(res.status).toBe(201);
    expect(turnsCol.updateOne).toHaveBeenCalled();
  });
});
