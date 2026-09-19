/**
 * cQuant API — Base client with AbortController, timeout, and retry support.
 *
 * This module provides:
 * - `request<T>()` — core fetch wrapper with error handling
 * - `requestWithRetry<T>()` — retry wrapper for transient failures
 * - `api` — convenience object with get/post/put/patch/delete methods
 */

import {
  ApiError,
  NetworkError,
  TimeoutError,
  AbortError,
  createApiErrorFromResponse,
  isRetryableError,
} from './errors'

// ── Constants ──────────────────────────────────────────────────────────────────

const BASE = '/api/v1'
const DEFAULT_TIMEOUT = 30_000 // 30 seconds

// ── Types ──────────────────────────────────────────────────────────────────────

export interface RequestConfig extends Omit<RequestInit, 'signal'> {
  /** Request timeout in milliseconds. Defaults to 30s. */
  timeout?: number
  /** External AbortSignal for request cancellation. */
  signal?: AbortSignal | null
  /** Skip JSON parsing and return raw Response. */
  raw?: boolean
}

export interface RetryConfig {
  /** Maximum number of retry attempts. Default: 3. */
  maxRetries?: number
  /** Base delay between retries in ms. Default: 1000. */
  baseDelay?: number
  /** Multiplier for exponential backoff. Default: 2. */
  backoffFactor?: number
  /** Maximum delay cap in ms. Default: 10000. */
  maxDelay?: number
  /** Function to determine if error is retryable. Default: isRetryableError. */
  shouldRetry?: (error: unknown, attempt: number) => boolean
}

// ── Core request function ──────────────────────────────────────────────────────

/**
 * Core fetch wrapper with AbortController, timeout, and structured error handling.
 *
 * @example
 * ```ts
 * const data = await request<User[]>('/users')
 * const created = await request<User>('/users', {
 *   method: 'POST',
 *   body: JSON.stringify({ name: 'Alice' }),
 * })
 * ```
 */
export async function request<T>(path: string, config: RequestConfig = {}): Promise<T> {
  const { timeout = DEFAULT_TIMEOUT, signal: externalSignal, raw = false, ...init } = config

  // Create an internal AbortController to combine timeout and external signal
  const controller = new AbortController()
  let timeoutId: ReturnType<typeof setTimeout> | undefined

  // Wire up external signal
  const onExternalAbort = () => controller.abort(externalSignal?.reason)
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort(externalSignal.reason)
    } else {
      externalSignal.addEventListener('abort', onExternalAbort, { once: true })
    }
  }

  // Wire up timeout
  if (timeout > 0) {
    timeoutId = setTimeout(() => controller.abort(new Error('timeout')), timeout)
  }

  try {
    // FormData bodies must NOT get a JSON Content-Type — the browser needs to
    // set multipart/form-data with a generated boundary. Setting it manually
    // strips the boundary and the server rejects the upload (422).
    const isFormData =
      typeof FormData !== 'undefined' && init.body instanceof FormData

    const baseHeaders = new Headers(init.headers)
    if (!isFormData && !baseHeaders.has('Content-Type')) {
      baseHeaders.set('Content-Type', 'application/json')
    }

    const res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: baseHeaders,
      signal: controller.signal,
    })

    // Clear timeout on success
    if (timeoutId) clearTimeout(timeoutId)

    // Handle non-OK responses
    if (!res.ok) {
      throw await createApiErrorFromResponse(res)
    }

    // Return raw Response if requested
    if (raw) {
      return res as unknown as T
    }

    // Parse JSON response
    return (await res.json()) as T
  } catch (error) {
    // Clear timeout on error
    if (timeoutId) clearTimeout(timeoutId)

    // Clean up external signal listener
    if (externalSignal) {
      externalSignal.removeEventListener('abort', onExternalAbort)
    }

    // Re-throw ApiError as-is
    if (error instanceof ApiError) {
      throw error
    }

    // Handle AbortError from AbortController
    if (error instanceof DOMException && error.name === 'AbortError') {
      // Check if external signal caused the abort
      if (externalSignal?.aborted) {
        throw new AbortError('请求已取消', error instanceof Error ? error : undefined)
      }
      // Timeout caused the abort
      throw new TimeoutError('请求超时', error instanceof Error ? error : undefined)
    }

    // Network errors (TypeError from fetch)
    if (error instanceof TypeError) {
      throw new NetworkError(error.message, error)
    }

    // Unknown error
    throw new ApiError(
      error instanceof Error ? error.message : '未知错误',
      { cause: error instanceof Error ? error : undefined },
    )
  }
}

// ── Retry wrapper ──────────────────────────────────────────────────────────────

/**
 * Wrapper that adds exponential backoff retry logic.
 *
 * @example
 * ```ts
 * const data = await requestWithRetry<User[]>('/users', {}, { maxRetries: 2 })
 * ```
 */
export async function requestWithRetry<T>(
  path: string,
  config: RequestConfig = {},
  retryConfig: RetryConfig = {},
): Promise<T> {
  const {
    maxRetries = 3,
    baseDelay = 1000,
    backoffFactor = 2,
    maxDelay = 10_000,
    shouldRetry = isRetryableError,
  } = retryConfig

  let lastError: unknown

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await request<T>(path, config)
    } catch (error) {
      lastError = error

      // Don't retry on last attempt or if not retryable
      if (attempt >= maxRetries || !shouldRetry(error, attempt)) {
        throw error
      }

      // Calculate delay with exponential backoff + jitter
      const delay = Math.min(
        baseDelay * Math.pow(backoffFactor, attempt) + Math.random() * 500,
        maxDelay,
      )

      // Respect Retry-After header if present
      if (error instanceof ApiError && error.status === 429) {
        const retryAfter = (error.details as Record<string, unknown>)?.['retry-after']
        if (typeof retryAfter === 'number') {
          await sleep(retryAfter * 1000)
          continue
        }
      }

      await sleep(delay)
    }
  }

  // This should never be reached, but TypeScript needs it
  throw lastError
}

// ── Convenience API ────────────────────────────────────────────────────────────

/**
 * Convenience methods for common HTTP verbs.
 *
 * @example
 * ```ts
 * const users = await api.get<User[]>('/users')
 * const user = await api.post<User>('/users', { name: 'Alice' })
 * await api.delete(`/users/${id}`)
 * ```
 */
export const api = {
  get: <T>(path: string, config?: Omit<RequestConfig, 'method' | 'body'>) =>
    request<T>(path, { ...config, method: 'GET' }),

  post: <T>(path: string, body?: unknown, config?: Omit<RequestConfig, 'method' | 'body'>) =>
    request<T>(path, {
      ...config,
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    }),

  put: <T>(path: string, body?: unknown, config?: Omit<RequestConfig, 'method' | 'body'>) =>
    request<T>(path, {
      ...config,
      method: 'PUT',
      body: body ? JSON.stringify(body) : undefined,
    }),

  patch: <T>(path: string, body?: unknown, config?: Omit<RequestConfig, 'method' | 'body'>) =>
    request<T>(path, {
      ...config,
      method: 'PATCH',
      body: body ? JSON.stringify(body) : undefined,
    }),

  delete: <T>(path: string, config?: Omit<RequestConfig, 'method'>) =>
    request<T>(path, { ...config, method: 'DELETE' }),
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
