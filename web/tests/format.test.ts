import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { parseUtc } from '~/composables/useApi'
import { formatDate, formatDuration, formatMoney, formatNumber, humanize, relativeTime } from '~/utils/format'

// Pin the process zone so a naive (zone-less) timestamp reads the same on every machine, CI included.
const originalTz = process.env.TZ
beforeAll(() => {
  process.env.TZ = 'America/New_York'
})
afterAll(() => {
  if (originalTz === undefined) delete process.env.TZ
  else process.env.TZ = originalTz
})

describe('formatDate', () => {
  it('formats a Zulu timestamp the same as the shared parser', () => {
    const expected = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
      parseUtc('2026-09-21T12:00:00Z'),
    )
    expect(formatDate('2026-09-21T12:00:00Z')).toBe(expected)
  })

  it('treats a zone-less timestamp as UTC, same as parseUtc', () => {
    const zoned = formatDate('2026-09-21T12:00:00Z')
    const naive = formatDate('2026-09-21T12:00:00')
    expect(naive).toBe(zoned)
    expect(naive).not.toBe(new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date('2026-09-21T12:00:00')))
  })

  it('falls back to the em dash on null', () => {
    expect(formatDate(null)).toBe('—')
  })

  it('falls back to the em dash on undefined', () => {
    expect(formatDate(undefined)).toBe('—')
  })

  it('falls back on unparsable input', () => {
    expect(formatDate('sometime last week')).toBe('—')
  })

  it('supports a custom fallback', () => {
    expect(formatDate(null, 'n/a')).toBe('n/a')
    expect(formatDate('garbage', 'n/a')).toBe('n/a')
  })
})

describe('relativeTime', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-21T12:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('collapses a moment in the past to "just now"', () => {
    expect(relativeTime('2026-09-21T11:59:40Z')).toBe('just now')
  })

  it('collapses a moment in the future to "just now"', () => {
    expect(relativeTime('2026-09-21T12:00:20Z')).toBe('just now')
  })

  it('formats minutes ago', () => {
    expect(relativeTime('2026-09-21T11:55:00Z')).toBe('5 min ago')
  })

  it('formats minutes in the future', () => {
    expect(relativeTime('2026-09-21T12:05:00Z')).toBe('in 5 min')
  })

  it('formats hours ago', () => {
    expect(relativeTime('2026-09-21T09:00:00Z')).toBe('3 h ago')
  })

  it('formats hours in the future', () => {
    expect(relativeTime('2026-09-21T14:00:00Z')).toBe('in 2 h')
  })

  it('formats a single day ago without pluralizing', () => {
    expect(relativeTime('2026-09-20T12:00:00Z')).toBe('1 day ago')
  })

  it('pluralizes multiple days ago', () => {
    expect(relativeTime('2026-09-18T12:00:00Z')).toBe('3 days ago')
  })

  it('pluralizes multiple days in the future', () => {
    expect(relativeTime('2026-09-24T12:00:00Z')).toBe('in 3 days')
  })

  it('falls back to the em dash on null', () => {
    expect(relativeTime(null)).toBe('—')
  })

  it('falls back on unparsable input', () => {
    expect(relativeTime('sometime last week')).toBe('—')
  })

  it('supports a custom fallback', () => {
    expect(relativeTime(undefined, 'n/a')).toBe('n/a')
  })
})

describe('formatDuration', () => {
  it('formats sub-second durations in milliseconds', () => {
    expect(formatDuration(850)).toBe('850 ms')
  })

  it('formats whole seconds', () => {
    expect(formatDuration(12000)).toBe('12 s')
  })

  it('formats minutes and seconds', () => {
    expect(formatDuration(185000)).toBe('3 min 5 s')
  })

  it('omits a zero seconds component', () => {
    expect(formatDuration(60000)).toBe('1 min')
  })

  it('formats hours and minutes', () => {
    expect(formatDuration(3720000)).toBe('1 h 2 min')
  })

  it('omits a zero minutes component', () => {
    expect(formatDuration(3600000)).toBe('1 h')
  })

  it('falls back to the em dash on null', () => {
    expect(formatDuration(null)).toBe('—')
  })

  it('falls back to the em dash on undefined', () => {
    expect(formatDuration(undefined)).toBe('—')
  })

  it('falls back on non-finite input', () => {
    expect(formatDuration(Number.NaN)).toBe('—')
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe('—')
  })
})

describe('formatMoney', () => {
  it('formats USD by default', () => {
    const expected = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(1234.5)
    expect(formatMoney(1234.5)).toBe(expected)
  })

  it('formats a zero amount', () => {
    const expected = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(0)
    expect(formatMoney(0)).toBe(expected)
  })

  it('formats other currencies', () => {
    const expected = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'EUR' }).format(10)
    expect(formatMoney(10, 'EUR')).toBe(expected)
  })

  it('falls back to the em dash on null', () => {
    expect(formatMoney(null)).toBe('—')
  })

  it('falls back to the em dash on undefined', () => {
    expect(formatMoney(undefined)).toBe('—')
  })

  it('falls back on non-finite input', () => {
    expect(formatMoney(Number.NaN)).toBe('—')
  })
})

describe('formatNumber', () => {
  it('groups thousands', () => {
    const expected = new Intl.NumberFormat(undefined).format(1234567)
    expect(formatNumber(1234567)).toBe(expected)
  })

  it('formats a zero value', () => {
    expect(formatNumber(0)).toBe(new Intl.NumberFormat(undefined).format(0))
  })

  it('falls back to the em dash on null', () => {
    expect(formatNumber(null)).toBe('—')
  })

  it('falls back to the em dash on undefined', () => {
    expect(formatNumber(undefined)).toBe('—')
  })

  it('falls back on non-finite input', () => {
    expect(formatNumber(Number.POSITIVE_INFINITY)).toBe('—')
  })
})

describe('humanize', () => {
  it('converts snake_case to Sentence case', () => {
    expect(humanize('last_seen')).toBe('Last seen')
  })

  it('converts kebab-case to Sentence case', () => {
    expect(humanize('manual-source')).toBe('Manual source')
  })

  it('capitalizes only the first word of a multi-word key', () => {
    expect(humanize('consecutive_failure_count')).toBe('Consecutive failure count')
  })

  it('lowercases an already-uppercase key before capitalizing the first letter', () => {
    expect(humanize('LAST_SEEN')).toBe('Last seen')
  })

  it('falls back to the em dash on null', () => {
    expect(humanize(null)).toBe('—')
  })

  it('falls back to the em dash on undefined', () => {
    expect(humanize(undefined)).toBe('—')
  })

  it('falls back on an empty string', () => {
    expect(humanize('')).toBe('—')
  })

  it('supports a custom fallback', () => {
    expect(humanize('', 'n/a')).toBe('n/a')
  })
})
