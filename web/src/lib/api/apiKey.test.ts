/**
 * API key storage helpers — localStorage round-trip + URL param attach.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  getApiKey,
  setApiKey,
  clearApiKey,
  withApiKeyParam,
  notifyUnauthorized,
  UNAUTHORIZED_EVENT,
} from './apiKey'

describe('apiKey helpers', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('set/get round-trips trimmed key', () => {
    setApiKey('  abc123  ')
    expect(getApiKey()).toBe('abc123')
  })

  it('empty string removes the key', () => {
    setApiKey('abc123')
    setApiKey('   ')
    expect(getApiKey()).toBe('')
  })

  it('clear removes the key', () => {
    setApiKey('abc123')
    clearApiKey()
    expect(getApiKey()).toBe('')
  })

  it('withApiKeyParam appends when key set', () => {
    setApiKey('k1')
    expect(withApiKeyParam('/api/v1/live/stream?s=a')).toBe(
      '/api/v1/live/stream?s=a&api_key=k1',
    )
    expect(withApiKeyParam('/api/v1/advisor/stream')).toBe(
      '/api/v1/advisor/stream?api_key=k1',
    )
  })

  it('withApiKeyParam is a no-op without key', () => {
    expect(withApiKeyParam('/api/v1/live/stream?s=a')).toBe('/api/v1/live/stream?s=a')
  })

  it('notifyUnauthorized dispatches deduped events', () => {
    const handler = vi.fn()
    window.addEventListener(UNAUTHORIZED_EVENT, handler)
    notifyUnauthorized()
    notifyUnauthorized() // within 10s dedupe window — swallowed
    expect(handler).toHaveBeenCalledTimes(1)
    window.removeEventListener(UNAUTHORIZED_EVENT, handler)
  })
})
