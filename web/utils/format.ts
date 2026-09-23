import { parseUtc } from '~/composables/useApi'

const FALLBACK = '—'

/** Parses an ISO timestamp, returning null when it is missing or unparsable. */
function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const date = parseUtc(iso)
  return Number.isNaN(date.getTime()) ? null : date
}

/**
 * Formats an ISO timestamp as a locale short date and time.
 * @param iso - ISO 8601 timestamp (with or without a zone), or null/undefined.
 * @param fallback - Text shown when iso is missing or unparsable.
 * @returns The formatted date and time, or fallback.
 */
export function formatDate(iso: string | null | undefined, fallback = FALLBACK): string {
  const date = toDate(iso)
  if (!date) return fallback
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

/**
 * Formats an ISO timestamp relative to now, e.g. "5 min ago" or "in 2 h". Anything within 45
 * seconds either side of now collapses to "just now".
 * @param iso - ISO 8601 timestamp (with or without a zone), or null/undefined.
 * @param fallback - Text shown when iso is missing or unparsable.
 * @returns A compact relative-time string, or fallback.
 */
export function relativeTime(iso: string | null | undefined, fallback = FALLBACK): string {
  const date = toDate(iso)
  if (!date) return fallback

  const diffMs = date.getTime() - Date.now()
  const absSeconds = Math.round(Math.abs(diffMs) / 1000)
  if (absSeconds < 45) return 'just now'

  let label: string
  const absMinutes = Math.round(absSeconds / 60)
  if (absMinutes < 60) {
    label = `${absMinutes} min`
  } else {
    const absHours = Math.round(absMinutes / 60)
    if (absHours < 24) {
      label = `${absHours} h`
    } else {
      const absDays = Math.round(absHours / 24)
      label = `${absDays} day${absDays === 1 ? '' : 's'}`
    }
  }
  return diffMs >= 0 ? `in ${label}` : `${label} ago`
}

/**
 * Formats a duration in milliseconds as a compact human string, e.g. "850 ms" or "3 min 5 s".
 * Zero-valued larger components are omitted (e.g. "1 h", not "1 h 0 min").
 * @param ms - Duration in milliseconds, or null/undefined.
 * @param fallback - Text shown when ms is missing or not a finite number.
 * @returns The formatted duration, or fallback.
 */
export function formatDuration(ms: number | null | undefined, fallback = FALLBACK): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return fallback
  const abs = Math.abs(ms)
  if (abs < 1000) return `${Math.round(abs)} ms`

  const totalSeconds = Math.round(abs / 1000)
  if (totalSeconds < 60) return `${totalSeconds} s`

  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return seconds > 0 ? `${minutes} min ${seconds} s` : `${minutes} min`

  const hours = Math.floor(minutes / 60)
  const remMinutes = minutes % 60
  return remMinutes > 0 ? `${hours} h ${remMinutes} min` : `${hours} h`
}

/**
 * Formats an amount as localized currency via Intl.NumberFormat.
 * @param amount - The numeric amount, or null/undefined.
 * @param currency - ISO 4217 currency code (default "USD").
 * @param fallback - Text shown when amount is missing or not a finite number.
 * @returns The formatted currency string, or fallback.
 */
export function formatMoney(amount: number | null | undefined, currency = 'USD', fallback = FALLBACK): string {
  if (amount === null || amount === undefined || !Number.isFinite(amount)) return fallback
  return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(amount)
}

/**
 * Formats a number with locale grouping via Intl.NumberFormat.
 * @param n - The number, or null/undefined.
 * @param fallback - Text shown when n is missing or not a finite number.
 * @returns The formatted number, or fallback.
 */
export function formatNumber(n: number | null | undefined, fallback = FALLBACK): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return fallback
  return new Intl.NumberFormat(undefined).format(n)
}

/**
 * Converts a snake_case or kebab-case key into a Sentence case label, e.g.
 * "last_seen" -> "Last seen", "manual-source" -> "Manual source". Only the first
 * word is capitalized.
 * @param key - The raw key, or null/undefined.
 * @param fallback - Text shown when key is missing or empty.
 * @returns The humanized label, or fallback.
 */
export function humanize(key: string | null | undefined, fallback = FALLBACK): string {
  if (!key) return fallback
  const words = key.trim().split(/[_-]+/).filter(Boolean).map((word) => word.toLowerCase())
  if (words.length === 0) return fallback
  const [first, ...rest] = words
  return [`${first.charAt(0).toUpperCase()}${first.slice(1)}`, ...rest].join(' ')
}
