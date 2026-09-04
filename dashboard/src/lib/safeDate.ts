/**
 * safeDate.ts — safe wrappers around the Date API.
 *
 * All functions accept string | number | Date | null | undefined.
 * They return "—" for null, undefined, NaN, empty string, or any input
 * that produces an invalid Date — so callers never see "Invalid Date".
 */

type DateInput = string | number | Date | null | undefined

function _toDate(input: DateInput): Date | null {
  if (input === null || input === undefined || input === '') return null
  const d = input instanceof Date ? input : new Date(input)
  if (isNaN(d.getTime())) return null
  return d
}

/**
 * Format a timestamp as a human-readable relative string: "3m ago", "2h ago", "5d ago".
 * Falls back to formatAbsolute for timestamps older than 7 days.
 * Returns "—" for invalid input.
 */
export function formatRelative(input: DateInput): string {
  const d = _toDate(input)
  if (!d) return '—'
  const diff = Math.floor((Date.now() - d.getTime()) / 1000)
  if (diff < 0) return formatAbsolute(d)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}d ago`
  return formatAbsolute(d)
}

/**
 * Format a timestamp as "YYYY-MM-DD HH:MM" in the local timezone.
 * Returns "—" for invalid input.
 */
export function formatAbsolute(input: DateInput): string {
  const d = _toDate(input)
  if (!d) return '—'
  try {
    return d.toLocaleString(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return '—'
  }
}

/**
 * Format a timestamp as a short date: "MM/DD/YYYY" (locale-dependent).
 * Returns "—" for invalid input.
 */
export function formatDate(input: DateInput): string {
  const d = _toDate(input)
  if (!d) return '—'
  try {
    return d.toLocaleDateString()
  } catch {
    return '—'
  }
}

/**
 * Format a timestamp as a time string: "HH:MM:SS".
 * Options mirror toLocaleTimeString options.
 * Returns "—" for invalid input.
 */
export function formatTime(
  input: DateInput,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d = _toDate(input)
  if (!d) return '—'
  try {
    return d.toLocaleTimeString(undefined, options)
  } catch {
    return '—'
  }
}

/**
 * Format a timestamp as a full locale string (date + time).
 * Returns "—" for invalid input.
 */
export function formatLocaleString(
  input: DateInput,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d = _toDate(input)
  if (!d) return '—'
  try {
    return d.toLocaleString(undefined, options)
  } catch {
    return '—'
  }
}
