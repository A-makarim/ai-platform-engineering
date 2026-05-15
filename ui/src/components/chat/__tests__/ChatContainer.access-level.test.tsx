/**
 * @jest-environment jsdom
 */
/**
 * ChatContainer access-level heuristic for autonomous threads.
 *
 * On first render (before the GET /api/chat/conversations/[id] response
 * lands), the panel should NOT flash read-only for an admin / admin-view
 * session viewing an autonomous thread. The local-store heuristic
 * pre-sets `accessLevel = 'autonomous_comment'`, which makes
 * `isReadOnly = false` in ChatContainer.
 *
 * Pinning this protects against regressions where the heuristic falls
 * back to `shared_readonly` or `admin_audit` for autonomous + admin
 * (the bug the access_level matrix was added to fix).
 */

import React from 'react';
import { render, waitFor } from '@testing-library/react';

const TEST_UUID = '11111111-1111-4111-8111-111111111111';

const dynamicAgentViewProps: Array<Record<string, unknown>> = [];
const supervisorViewProps: Array<Record<string, unknown>> = [];

let currentSession: {
  user: { name: string; email: string };
  role?: string;
  canViewAdmin?: boolean;
} = {
  user: { name: 'admin', email: 'admin@example.com' },
  role: 'admin',
  canViewAdmin: true,
};

jest.mock('next-auth/react', () => ({
  useSession: () => ({ data: currentSession, status: 'authenticated' }),
}));

jest.mock('next/navigation', () => ({
  useParams: () => ({ uuid: TEST_UUID }),
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

jest.mock('@/lib/config', () => ({
  getConfig: (key: string) => {
    const configs: Record<string, unknown> = {
      caipeUrl: 'http://localhost:8000',
      dynamicAgentsUrl: 'http://localhost:8001',
      dynamicAgentsEnabled: true,
    };
    return configs[key];
  },
}));

jest.mock('@/lib/storage-config', () => ({
  getStorageMode: () => 'mongodb',
  shouldUseLocalStorage: () => false,
}));

jest.mock('@/lib/api-client', () => ({
  apiClient: {
    // Intentionally never resolves — we want to assert on the *initial*
    // accessLevel state set by the local-store heuristic, not the value
    // that lands after the GET response.
    getConversation: jest.fn().mockReturnValue(new Promise(() => {})),
    getMessages: jest.fn().mockResolvedValue({ items: [], total: 0 }),
  },
}));

const autonomousConv: any = {
  id: TEST_UUID,
  title: 'Scheduled task',
  source: 'autonomous',
  task_id: 'task-xyz',
  createdAt: new Date(0),
  updatedAt: new Date(0),
  messages: [
    {
      id: 'task:task-xyz:creation_intent',
      role: 'user',
      content: 'created',
      timestamp: new Date(0),
      events: [],
      isFinal: true,
    },
  ],
  a2aEvents: [],
  streamEvents: [],
  participants: [],
};

jest.mock('@/store/chat-store', () => {
  const state = {
    conversations: [autonomousConv],
    setActiveConversation: jest.fn(),
    loadMessagesFromServer: jest.fn().mockResolvedValue(undefined),
    loadTurnsFromServer: jest.fn().mockResolvedValue(undefined),
  };
  const useChatStore = ((selector?: (s: typeof state) => unknown) => {
    if (selector) return selector(state);
    return state;
  }) as unknown as {
    (): typeof state;
    (selector: (s: typeof state) => unknown): unknown;
    getState(): typeof state;
    setState(...args: unknown[]): void;
  };
  useChatStore.getState = () => state;
  useChatStore.setState = jest.fn();
  return { useChatStore };
});

jest.mock('@/components/chat/PlatformEngineerChatView', () => ({
  SupervisorChatView: (props: Record<string, unknown>) => {
    supervisorViewProps.push(props);
    return <div data-testid="supervisor-view" />;
  },
}));
jest.mock('@/components/chat/DynamicAgentChatView', () => ({
  ChatView: (props: Record<string, unknown>) => {
    dynamicAgentViewProps.push(props);
    return <div data-testid="dynamic-agent-view" />;
  },
}));
jest.mock('@/components/ui/caipe-spinner', () => ({
  CAIPESpinner: () => <div data-testid="spinner" />,
}));

jest.spyOn(console, 'log').mockImplementation(() => {});
jest.spyOn(console, 'warn').mockImplementation(() => {});

import { ChatContainer } from '../ChatContainer';

beforeEach(() => {
  dynamicAgentViewProps.length = 0;
  supervisorViewProps.length = 0;
});

describe('ChatContainer — autonomous access-level heuristic', () => {
  it('does NOT render the panel as read-only for admin viewing an autonomous thread', async () => {
    currentSession = {
      user: { name: 'admin', email: 'admin@example.com' },
      role: 'admin',
      canViewAdmin: true,
    };

    render(<ChatContainer />);

    await waitFor(() => {
      // SupervisorChatView is the rendered child for this fixture (no agent participant).
      expect(supervisorViewProps.length).toBeGreaterThan(0);
    });

    // First render must already be writable — no flash to admin_audit.
    const firstProps = supervisorViewProps[0];
    expect(firstProps.readOnly).toBe(false);
    expect(firstProps.readOnlyReason).toBeUndefined();
  });

  it('does NOT render the panel as read-only for admin-view (canViewAdmin) viewing an autonomous thread', async () => {
    currentSession = {
      user: { name: 'auditor', email: 'auditor@example.com' },
      role: 'user',
      canViewAdmin: true,
    };

    render(<ChatContainer />);

    await waitFor(() => {
      expect(supervisorViewProps.length).toBeGreaterThan(0);
    });

    expect(supervisorViewProps[0].readOnly).toBe(false);
    expect(supervisorViewProps[0].readOnlyReason).toBeUndefined();
  });

  it('does NOT pre-set autonomous_comment for a plain user viewing an autonomous thread', async () => {
    currentSession = {
      user: { name: 'plain', email: 'plain@example.com' },
      role: 'user',
      canViewAdmin: false,
    };

    render(<ChatContainer />);

    await waitFor(() => {
      expect(supervisorViewProps.length).toBeGreaterThan(0);
    });

    // The heuristic only stamps autonomous_comment for admin / admin-view.
    // For a plain user, the access level stays null until the server GET
    // responds — the user-visible invariant is that the readOnlyReason
    // must NEVER be 'autonomous_comment' (writable). The server-side
    // shared_readonly mapping is exercised in api-middleware tests.
    expect(supervisorViewProps[0].readOnlyReason).not.toBe('autonomous_comment');
  });
});
