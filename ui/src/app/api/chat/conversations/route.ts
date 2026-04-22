// GET /api/chat/conversations - List user's conversations
// POST /api/chat/conversations - Create new conversation

import { NextRequest, NextResponse } from 'next/server';
import { v4 as uuidv4 } from 'uuid';
import { getCollection, isMongoDBConfigured } from '@/lib/mongodb';
import {
  withAuth,
  withErrorHandler,
  successResponse,
  paginatedResponse,
  validateRequired,
  getPaginationParams,
  getUserTeamIds,
} from '@/lib/api-middleware';
import type { Conversation, CreateConversationRequest } from '@/types/mongodb';

// GET /api/chat/conversations
export const GET = withErrorHandler(async (request: NextRequest) => {
  // Check if MongoDB is configured
  if (!isMongoDBConfigured) {
    return NextResponse.json(
      {
        success: false,
        error: 'MongoDB not configured - use localStorage mode',
        code: 'MONGODB_NOT_CONFIGURED',
      },
      { status: 503 } // Service Unavailable
    );
  }

  return withAuth(request, async (req, user) => {
    const { page, pageSize, skip } = getPaginationParams(request);
    const url = new URL(request.url);
    const archived = url.searchParams.get('archived') === 'true';
    const pinned = url.searchParams.get('pinned') === 'true';
    // Allow-list of source filters. We deliberately do NOT honor 'slack'
    // here so that the existing default exclusion of slack-originated
    // conversations remains the only way slack rows appear (in their
    // own dedicated view).
    const sourceParam = url.searchParams.get('source');
    const sourceFilter =
      sourceParam === 'autonomous' || sourceParam === 'web' ? sourceParam : null;

    const conversations = await getCollection<Conversation>('conversations');

    // Build query — exclude soft-deleted and (by default) Slack conversations.
    // Autonomous conversations are written by the autonomous_agents service
    // under a synthetic owner (e.g., 'autonomous@system'), so applying the
    // normal per-user ownership/sharing filter would hide them from every
    // human user. When the caller explicitly opts in via `?source=autonomous`
    // we treat the listing as an operator/audit view: any authenticated user
    // can read autonomous run conversations. We still hard-pin
    // `source: 'autonomous'` server-side to prevent the query parameter from
    // being abused to bypass per-user authorization on human conversations.
    const query: any = {
      $and: [
        { $or: [{ deleted_at: null }, { deleted_at: { $exists: false } }] },
      ],
    };

    if (sourceFilter === 'autonomous') {
      query.source = 'autonomous';
    } else {
      // Default human/web view: keep the existing ownership/sharing filter
      // and continue excluding slack-sourced rows.
      const userTeamIds = await getUserTeamIds(user.email);
      const ownershipConditions: any[] = [
        { owner_id: user.email },
        { 'sharing.shared_with': user.email },
        { 'sharing.is_public': true },
      ];
      if (userTeamIds.length > 0) {
        ownershipConditions.push({
          'sharing.shared_with_teams': { $in: userTeamIds },
        });
      }
      query.$or = ownershipConditions;
      query.source = { $nin: ['slack', 'autonomous'] };

      if (sourceFilter === 'web') {
        // Tighten further when the caller wants pure web-only.
        // Conversations created via the existing POST handler do NOT
        // currently set a ``source`` field, so a strict
        // ``source: 'web'`` match would silently hide every legacy /
        // freshly-created human chat. Treat missing/null as 'web'.
        query.source = {
          $in: ['web', null],
        } as { $in: (string | null)[] };
        // Mongo's ``$in`` with ``null`` matches both explicit nulls
        // and missing fields, so legacy and new web conversations
        // both come through.
      }
    }

    if (archived !== null) {
      query.is_archived = archived;
    }

    if (pinned) {
      query.is_pinned = true;
    }

    // Get total count
    const total = await conversations.countDocuments(query);

    // Get paginated results
    const items = await conversations
      .find(query)
      .sort({ is_pinned: -1, updated_at: -1 })
      .skip(skip)
      .limit(pageSize)
      .toArray();

    return paginatedResponse(items, total, page, pageSize);
  });
});

// POST /api/chat/conversations
export const POST = withErrorHandler(async (request: NextRequest) => {
  // Check if MongoDB is configured
  if (!isMongoDBConfigured) {
    return NextResponse.json(
      {
        success: false,
        error: 'MongoDB not configured - use localStorage mode',
        code: 'MONGODB_NOT_CONFIGURED',
      },
      { status: 503 } // Service Unavailable
    );
  }

  return withAuth(request, async (req, user) => {
    const body: CreateConversationRequest = await request.json();

    validateRequired(body, ['title']);

    const conversations = await getCollection<Conversation>('conversations');

    const now = new Date();
    const newConversation: Conversation = {
      _id: body.id || uuidv4(), // Use client-provided ID if given, otherwise generate
      title: body.title,
      owner_id: user.email,
      agent_id: body.agent_id, // Dynamic agent ID; undefined = Platform Engineer
      created_at: now,
      updated_at: now,
      metadata: {
        agent_version: process.env.npm_package_version || '0.1.0',
        model_used: 'gpt-4o',
        total_messages: 0,
      },
      sharing: {
        is_public: false,
        shared_with: [],
        shared_with_teams: [],
        share_link_enabled: false,
      },
      tags: body.tags || [],
      is_archived: false,
      is_pinned: false,
    };

    await conversations.insertOne(newConversation);

    return successResponse(newConversation, 201);
  });
});
