/**
 * cQuant API — AI Advisor domain.
 */

import { api, type RequestConfig } from './client'
import { withApiKeyParam } from './apiKey'

// ── Types (not yet in types/) ────────────────────────────────────────────────

export interface ChatResponse {
  response: string
  session_id: string
  artifacts: string[]
}

// ── API ────────────────────────────────────────────────────────────────────

export const advisorApi = {
  chat: (message: string, sessionId?: string, config?: RequestConfig) =>
    api.post<ChatResponse>(
      '/advisor/chat',
      { message, session_id: sessionId ?? '' },
      config,
    ),

  report: (subject: string, sessionId?: string, config?: RequestConfig) =>
    api.post<{ report: string; session_id: string; artifacts: string[] }>(
      '/advisor/report',
      { subject, session_id: sessionId ?? '' },
      config,
    ),

  // Extended
  session: (id: string, config?: RequestConfig) =>
    api.get<{
      session_id: string
      turn_count: number
      history: Record<string, unknown>[]
    }>(`/advisor/sessions/${id}`, config),

  sessionAgents: (id: string, config?: RequestConfig) =>
    api.get<{
      items: { agent_role: string; content: string; artifacts: string[] }[]
    }>(`/advisor/sessions/${id}/agents`, config),

  streamUrl: (message: string, sessionId?: string) => {
    const params = new URLSearchParams({ message })
    if (sessionId) params.set('session_id', sessionId)
    return withApiKeyParam(`/api/v1/advisor/stream?${params}`)
  },
}

// ── Backward-compatible alias ───────────────────────────────────────────────

/** @deprecated Use `advisorApi` which now includes session/sessionAgents/streamUrl. */
export const advisorExtApi = {
  session: advisorApi.session,
  sessionAgents: advisorApi.sessionAgents,
  streamUrl: advisorApi.streamUrl,
}
