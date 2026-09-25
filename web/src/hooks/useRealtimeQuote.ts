import { useEffect, useRef, useState, useCallback } from 'react'
import type { RealtimeQuote } from '@/lib/api'
import { withApiKeyParam } from '@/lib/api/apiKey'

interface UseRealtimeQuoteOptions {
  symbols: string[]
  interval?: number
  enabled?: boolean
}

interface UseRealtimeQuoteResult {
  quotes: Record<string, RealtimeQuote>
  connected: boolean
  error: string | null
  lastUpdate: string | null
}

/**
 * SSE hook for real-time quote streaming.
 * Connects to /api/v1/live/stream and updates quotes in real-time.
 */
export function useRealtimeQuote({
  symbols,
  interval = 5,
  enabled = true,
}: UseRealtimeQuoteOptions): UseRealtimeQuoteResult {
  const [quotes, setQuotes] = useState<Record<string, RealtimeQuote>>({})
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<string | null>(null)
  const eventSourceRef = useRef<EventSource | null>(null)

  const connect = useCallback(() => {
    if (!enabled || symbols.length === 0) return

    const params = new URLSearchParams({
      symbols: symbols.join(','),
      interval: String(interval),
    })
    const url = withApiKeyParam(`/api/v1/live/stream?${params}`)

    const eventSource = new EventSource(url)
    eventSourceRef.current = eventSource

    eventSource.onopen = () => {
      setConnected(true)
      setError(null)
    }

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'quotes' && data.items) {
          setQuotes(data.items)
          setLastUpdate(data.timestamp)
        } else if (data.type === 'error') {
          setError(data.message)
        }
      } catch {
        // Ignore parse errors
      }
    }

    eventSource.onerror = () => {
      setConnected(false)
      setError('Connection lost')
      eventSource.close()
      // Reconnect after 3 seconds
      setTimeout(connect, 3000)
    }
  }, [symbols, interval, enabled])

  useEffect(() => {
    connect()

    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close()
        eventSourceRef.current = null
      }
    }
  }, [connect])

  return { quotes, connected, error, lastUpdate }
}
