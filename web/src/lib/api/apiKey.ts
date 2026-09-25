/**
 * API key storage & unauthorized-event helpers.
 *
 * Zero-dependency leaf module — imported by both `client.ts` (header attach,
 * 401 event) and `api/auth.ts` (status/verify calls), so it must not import
 * the API client itself (circular import).
 */

const STORAGE_KEY = 'cquant.apiKey'

/** Debounce window for the unauthorized event — one toast per burst of 401s. */
const UNAUTHORIZED_DEDUPE_MS = 10_000

export const UNAUTHORIZED_EVENT = 'cquant:unauthorized'

export function getApiKey(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setApiKey(key: string): void {
  try {
    const trimmed = key.trim()
    if (trimmed) localStorage.setItem(STORAGE_KEY, trimmed)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    // localStorage unavailable (private mode etc.) — key simply won't persist
  }
}

export function clearApiKey(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
}

/**
 * Append `api_key=` to a URL for connections that cannot set headers
 * (EventSource). No-op when no key is configured.
 */
export function withApiKeyParam(url: string): string {
  const key = getApiKey()
  if (!key) return url
  return url + (url.includes('?') ? '&' : '?') + 'api_key=' + encodeURIComponent(key)
}

let lastUnauthorizedAt = 0

/**
 * Notify the app that a request got 401. Deduplicated so a page firing a
 * dozen queries against a missing key produces a single guidance toast.
 */
export function notifyUnauthorized(): void {
  const now = Date.now()
  if (now - lastUnauthorizedAt < UNAUTHORIZED_DEDUPE_MS) return
  lastUnauthorizedAt = now
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
}
