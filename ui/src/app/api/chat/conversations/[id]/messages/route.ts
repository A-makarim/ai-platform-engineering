// GET /api/chat/conversations/[id]/messages - Get all messages in conversation
//   Kept for reading legacy `messages` data during migration (Phase 3).
// POST /api/chat/conversations/[id]/messages - Add message to conversation
//   Kept for migration tooling. The UI no longer calls this — the A2A server
//   persists all streaming data directly (Phase 1/3). Future cleanup: remove
//   POST once all conversations have been migrated to server-side persistence.

import { NextRequest } from 'next/server';
import { getCollection } from '@/lib/mongodb';
import {
  withAuth,
  withErrorHandler,
  successResponse,
  paginatedResponse,
  ApiError,
  requireConversationAccess,
  validateUUID,
  validateRequired,
  getPaginationParams,
} from '@/lib/api-middleware';
import type { Message, AddMessageRequest, Conversation } from '@/types/mongodb';
import { getAgentId } from '@/types/a2a';

// Anchored UUIDv4-shape regexes for autonomous_comment id validation.
// The autonomous follow-up surface lives on a single conversation and writes
// flow only from privileged users (admin / admin-view), so we restrict
// `message_id` and `metadata.turn_id` to a narrow namespace to prevent
// collision with publisher-generated ids (`run:`, `task:`, `autonomous:`).
const UUID_RE = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const AUTONOMOUS_MSG_ID_RE = new RegExp(`^(manual|user|asst):${UUID_RE}$`, 'i');
const AUTONOMOUS_TURN_ID_RE = new RegExp(`^manual-${UUID_RE}$`, 'i');

const SUPERVISOR_FALLBACK_SENDER = 'supervisor';

/**
 * Structured logger for autonomous_comment denial / validation paths.
 * Emits a single JSON line per event with stable codes so dashboards and
 * alerts can pivot on `event` and `code`. Never includes message content
 * or sender values.
 */
function logAutonomousCommentEvent(
  event: 'autonomous_comment.denied' | 'autonomous_comment.invalid' | 'autonomous_comment.duplicate',
  data: {
    code: string;
    userId: string;
    conversationId: string;
    actor_role: 'admin' | 'admin_view' | 'user';
  },
): void {
  console.warn(JSON.stringify({ event, ...data }));
}

function resolveActorRole(session: { role?: string; canViewAdmin?: boolean } | undefined): 'admin' | 'admin_view' | 'user' {
  if (session?.role === 'admin') return 'admin';
  if (session?.canViewAdmin === true) return 'admin_view';
  return 'user';
}

// GET /api/chat/conversations/[id]/messages
export const GET = withErrorHandler(async (
  request: NextRequest,
  context: { params: Promise<{ id: string }> }
) => {
  return withAuth(request, async (req, user, session) => {
    const params = await context.params;
    const conversationId = params.id;

    if (!validateUUID(conversationId)) {
      throw new ApiError('Invalid conversation ID format', 400);
    }

    // Verify user has access (admins get read-only audit access)
    await requireConversationAccess(conversationId, user.email, getCollection, session);

    const { page, pageSize, skip } = getPaginationParams(request);

    const messages = await getCollection<Message>('messages');

    const total = await messages.countDocuments({ conversation_id: conversationId });

    const items = await messages
      .find({ conversation_id: conversationId })
      .sort({ created_at: 1 })
      .skip(skip)
      .limit(pageSize)
      .toArray();

    return paginatedResponse(items, total, page, pageSize);
  });
});

// POST /api/chat/conversations/[id]/messages
// Uses UPSERT on message_id: if a message with this client-generated ID already
// exists, it is updated (content, metadata, events). Idempotent — safe to call
// multiple times for the same message without duplicating rows.
export const POST = withErrorHandler(async (
  request: NextRequest,
  context: { params: Promise<{ id: string }> }
) => {
  return withAuth(request, async (req, user, session) => {
    const params = await context.params;
    const conversationId = params.id;
    const body: AddMessageRequest = await request.json();

    if (!validateUUID(conversationId)) {
      throw new ApiError('Invalid conversation ID format', 400);
    }

    validateRequired(body, ['role', 'content']);

    // Verify user has access and get conversation for owner_id
    const { access_level, conversation: convFromAccess } = await requireConversationAccess(
      conversationId, user.email, getCollection, session
    );

    // Read-only access — block writes
    if (access_level === 'admin_audit' || access_level === 'shared_readonly') {
      logAutonomousCommentEvent('autonomous_comment.denied', {
        code: 'READ_ONLY',
        userId: user.email,
        conversationId,
        actor_role: resolveActorRole(session),
      });
      throw new ApiError('Read-only access — cannot add messages', 403, 'FORBIDDEN');
    }

    // ──────────────────────────────────────────────────────────────────
    // Autonomous comment branch: strict id/role/turn_id validation,
    // server-stamped sender + metadata, insert-only persistence.
    // Privileged viewers (admin / admin-view) post follow-ups against a
    // publisher-owned conversation; we never want a client value to pollute
    // the namespace owned by services/chat_history.py.
    // ──────────────────────────────────────────────────────────────────
    if (access_level === 'autonomous_comment') {
      return await handleAutonomousCommentInsert({
        body,
        conversationId,
        conversation: convFromAccess,
        user,
        session,
      });
    }

    const conversations = await getCollection<Conversation>('conversations');
    const conversation = await conversations.findOne({ _id: conversationId });
    const ownerId = conversation?.owner_id || user.email;

    const messages = await getCollection<Message>('messages');

    const now = new Date();

    // Resolve sender identity for user messages.
    // If the client provides sender fields, use them. Otherwise, fall back to
    // the authenticated session user. This ensures shared conversations correctly
    // attribute each message to the person who typed it.
    const senderEmail = body.sender_email || (body.role === 'user' ? user.email : undefined);
    const senderName = body.sender_name || (body.role === 'user' ? user.name : undefined);
    const senderImage = body.sender_image || undefined;

    // Upsert: update if message_id exists, insert otherwise.
    // $set updates content/metadata/events on every call (idempotent).
    // $setOnInsert sets immutable fields only on first insert.
    const result = await messages.updateOne(
      { message_id: body.message_id, conversation_id: conversationId },
      {
        $set: {
          content: body.content,
          metadata: {
            source: 'web',
            turn_id: body.metadata?.turn_id || `turn-${Date.now()}`,
            model: body.metadata?.model,
            tokens_used: body.metadata?.tokens_used,
            latency_ms: body.metadata?.latency_ms,
            agent_name: body.metadata?.agent_name,
            is_final: body.metadata?.is_final,
            ...(body.metadata?.turn_status && { turn_status: body.metadata.turn_status }),
            ...(body.metadata?.is_interrupted && { is_interrupted: body.metadata.is_interrupted }),
            ...(body.metadata?.task_id && { task_id: body.metadata.task_id }),
            ...(body.metadata?.timeline_segments && { timeline_segments: body.metadata.timeline_segments }),
          },
          ...(body.a2a_events !== undefined && { a2a_events: body.a2a_events }),
          ...(body.stream_events !== undefined && { stream_events: body.stream_events }),
          ...(body.artifacts !== undefined && { artifacts: body.artifacts }),
          updated_at: now,
        },
        $setOnInsert: {
          message_id: body.message_id,
          conversation_id: conversationId,
          owner_id: ownerId,
          role: body.role,
          created_at: now,
          // Sender identity — set only on insert (immutable per message)
          ...(senderEmail && { sender_email: senderEmail }),
          ...(senderName && { sender_name: senderName }),
          ...(senderImage && { sender_image: senderImage }),
        },
      },
      { upsert: true }
    );

    // Only increment total_messages on new inserts (not updates)
    if (result.upsertedId) {
      await conversations.updateOne(
        { _id: conversationId },
        {
          $set: { updated_at: now },
          $inc: { 'metadata.total_messages': 1 },
        }
      );
    } else {
      // Just update timestamp for updates
      await conversations.updateOne(
        { _id: conversationId },
        { $set: { updated_at: now } }
      );
    }

    const upserted = await messages.findOne(
      { message_id: body.message_id, conversation_id: conversationId }
    );

    return successResponse(upserted, result.upsertedId ? 201 : 200);
  });
});

/**
 * Strict insert-only handler for `autonomous_comment` follow-ups.
 *
 * Accepts both `role: "user"` (manual operator follow-up) and `role: "assistant"`
 * (the supervisor's streamed reply persisted client-side). Sender identity and
 * metadata are stamped server-side so a privileged viewer cannot spoof a
 * supervisor reply.
 *
 * Persistence uses `$setOnInsert` only — no `$set` — so a re-POST with the same
 * `message_id` returns 409 instead of overwriting prior content.
 */
async function handleAutonomousCommentInsert(args: {
  body: AddMessageRequest;
  conversationId: string;
  conversation: any;
  user: { email: string; name: string };
  session: { role?: string; canViewAdmin?: boolean } | undefined;
}) {
  const { body, conversationId, conversation, user, session } = args;
  const actor_role = resolveActorRole(session);

  // Validate role allowlist (reject system, etc.)
  if (body.role !== 'user' && body.role !== 'assistant') {
    logAutonomousCommentEvent('autonomous_comment.invalid', {
      code: 'AUTONOMOUS_COMMENT_BAD_ROLE',
      userId: user.email,
      conversationId,
      actor_role,
    });
    throw new ApiError(
      'autonomous_comment role must be "user" or "assistant"',
      400,
      'AUTONOMOUS_COMMENT_BAD_ROLE',
    );
  }

  // Validate message_id namespace (manual: / user: / asst: only).
  if (typeof body.message_id !== 'string' || !AUTONOMOUS_MSG_ID_RE.test(body.message_id)) {
    logAutonomousCommentEvent('autonomous_comment.invalid', {
      code: 'AUTONOMOUS_COMMENT_BAD_NAMESPACE',
      userId: user.email,
      conversationId,
      actor_role,
    });
    throw new ApiError(
      'autonomous_comment message_id must match (manual|user|asst):<uuid>',
      400,
      'AUTONOMOUS_COMMENT_BAD_NAMESPACE',
    );
  }

  // Validate metadata.turn_id namespace (manual-<uuid> only).
  const turnId = body.metadata?.turn_id;
  if (typeof turnId !== 'string' || !AUTONOMOUS_TURN_ID_RE.test(turnId)) {
    logAutonomousCommentEvent('autonomous_comment.invalid', {
      code: 'AUTONOMOUS_COMMENT_BAD_TURN_ID',
      userId: user.email,
      conversationId,
      actor_role,
    });
    throw new ApiError(
      'autonomous_comment metadata.turn_id must match manual-<uuid>',
      400,
      'AUTONOMOUS_COMMENT_BAD_TURN_ID',
    );
  }

  // Resolve sender identity server-side. Client sender_* fields are ignored.
  let senderEmail: string | undefined;
  let senderName: string | undefined;
  let senderImage: string | undefined;
  if (body.role === 'user') {
    senderEmail = user.email;
    senderName = user.name;
  } else {
    // Assistant: stamp from agent participant; fall back to a stable literal
    // when the conversation has no agent participant (Platform Engineer threads).
    const agentId = getAgentId(conversation || {});
    senderEmail = agentId || SUPERVISOR_FALLBACK_SENDER;
    senderName = agentId || SUPERVISOR_FALLBACK_SENDER;
  }

  const now = new Date();
  const ownerId = conversation?.owner_id || user.email;
  const messages = await getCollection<Message>('messages');
  const conversations = await getCollection<Conversation>('conversations');

  // Server-stamped metadata. Drops any other client metadata silently.
  const stampedMetadata: Record<string, unknown> = {
    source: 'web',
    turn_id: turnId,
    kind: body.role === 'user' ? 'manual_followup' : 'manual_followup_response',
    created_via_access_level: 'autonomous_comment',
    actor_role,
    ...(conversation?.task_id && { task_id: conversation.task_id }),
  };

  const insertDoc: Record<string, unknown> = {
    message_id: body.message_id,
    conversation_id: conversationId,
    owner_id: ownerId,
    role: body.role,
    content: body.content,
    metadata: stampedMetadata,
    created_at: now,
    updated_at: now,
    ...(senderEmail && { sender_email: senderEmail }),
    ...(senderName && { sender_name: senderName }),
    ...(senderImage && { sender_image: senderImage }),
  };

  const result = await messages.updateOne(
    { message_id: body.message_id, conversation_id: conversationId },
    { $setOnInsert: insertDoc },
    { upsert: true },
  );

  // No upsertedId means an existing doc matched — reject as duplicate.
  if (!result.upsertedId) {
    logAutonomousCommentEvent('autonomous_comment.duplicate', {
      code: 'MESSAGE_ALREADY_EXISTS',
      userId: user.email,
      conversationId,
      actor_role,
    });
    throw new ApiError(
      'A message with this message_id already exists',
      409,
      'MESSAGE_ALREADY_EXISTS',
    );
  }

  // Mirror the owner/shared write path: bump updated_at + total_messages so the
  // sidebar resorts the autonomous thread on a new follow-up.
  await conversations.updateOne(
    { _id: conversationId },
    {
      $set: { updated_at: now },
      $inc: { 'metadata.total_messages': 1 },
    },
  );

  const upserted = await messages.findOne(
    { message_id: body.message_id, conversation_id: conversationId },
  );

  return successResponse(upserted, 201);
}
