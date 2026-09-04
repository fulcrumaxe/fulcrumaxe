/**
 * useEtaggedPoll — ETag/304-aware polling hook.
 *
 * Calls the provided async fetcher every `intervalMs` milliseconds.
 * On the first call, the fetcher receives an empty string etag.
 * On subsequent calls, it receives the last known etag value.
 * When the fetcher returns { not_modified: true }, the data is unchanged
 * and the existing state is preserved.
 *
 * Usage:
 *   const { data, loading, error } = useEtaggedPoll(
 *     async (etag) => jsonRpc('fleet.cost', { if_none_match: etag }),
 *     10_000
 *   )
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface EtaggedResponse {
  not_modified?: boolean
  etag?: string
  [key: string]: unknown
}

export interface PollState<T> {
  data: T | null
  loading: boolean
  error: string | null
}

export function useEtaggedPoll<T extends EtaggedResponse>(
  fetcher: (etag: string) => Promise<T>,
  intervalMs = 10_000,
): PollState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const etagRef = useRef<string>('')
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchOnce = useCallback(async () => {
    try {
      const resp = await fetcher(etagRef.current)
      if (resp.not_modified) {
        // 304-equivalent — keep existing data
        return
      }
      if (resp.etag) {
        etagRef.current = resp.etag
      }
      setData(resp)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [fetcher])

  useEffect(() => {
    fetchOnce()
    intervalRef.current = setInterval(fetchOnce, intervalMs)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [fetchOnce, intervalMs])

  return { data, loading, error }
}
