import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import type { Component } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import HistoryPage from '~/pages/history.vue'
import DashboardPage from '~/pages/index.vue'
import { parseUtc } from '~/composables/useApi'
import type { RunSummary, StatusResponse } from '~/composables/useApi'

// The old code read a zone-less timestamp as local time, so it only differs from parseUtc when the
// process zone is not UTC. Pin one so the tests fail on the old code on any machine, CI included.
const originalTz = process.env.TZ
beforeAll(() => {
  process.env.TZ = 'America/New_York'
})
afterAll(() => {
  if (originalTz === undefined) delete process.env.TZ
  else process.env.TZ = originalTz
})

const ZONELESS = '2026-09-21T12:00:00'
const ZULU = '2026-09-21T12:00:00Z'
const GARBAGE = 'sometime last week'

function run(id: number, startedAt: string): RunSummary {
  return {
    id,
    trigger: 'manual',
    status: 'success',
    started_at: startedAt,
    finished_at: null,
    summary: `summary ${id}`,
    error: null,
    has_pdf: false,
  }
}

let runs: RunSummary[]
let status: StatusResponse

beforeEach(() => {
  runs = []
  status = { last_run: null, next_scheduled_run: null, source_health: [] }
  vi.stubGlobal('$fetch', async (url: string) => {
    const path = new URL(url).pathname
    if (path === '/api/runs') return runs
    if (path === '/api/status') return status
    throw new Error(`unexpected GET ${path}`)
  })
})

let mounted: Array<VueWrapper<unknown>> = []

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

// QPage renders nothing outside a QLayout, so a page is mounted in the same shell app.vue gives it.
async function mountPage(page: Component) {
  const Harness = defineComponent({
    render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(page))),
  })
  const wrapper = await mountSuspended(Harness, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

const cells = () => [...document.body.querySelectorAll<HTMLElement>('td')].map((td) => td.textContent?.trim())
const pageText = () => document.body.textContent ?? ''

describe('test setup', () => {
  it('runs in a non-UTC zone where a zone-less string parsed as local time is a different instant', () => {
    expect(new Date(ZONELESS).getTimezoneOffset()).not.toBe(0)
    expect(parseUtc(ZONELESS).toLocaleString()).not.toBe(new Date(ZONELESS).toLocaleString())
  })
})

describe('history page', () => {
  it('renders a Z-suffixed started_at as the instant parseUtc gives', async () => {
    runs = [run(1, ZULU)]
    await mountPage(HistoryPage)
    expect(cells()).toContain(parseUtc(ZULU).toLocaleString())
  })

  it('treats a zone-less started_at as UTC, not as viewer-local time', async () => {
    runs = [run(2, ZONELESS)]
    await mountPage(HistoryPage)
    expect(cells()).toContain(parseUtc(ZONELESS).toLocaleString())
    expect(cells()).not.toContain(new Date(ZONELESS).toLocaleString())
  })

  it('renders the same instant for the zone-less and the Z form', async () => {
    runs = [run(3, ZONELESS), run(4, ZULU)]
    await mountPage(HistoryPage)
    const expected = parseUtc(ZULU).toLocaleString()
    expect(cells().filter((text) => text === expected)).toHaveLength(2)
  })

  it('renders an unparseable started_at as the raw text, never Invalid Date', async () => {
    runs = [run(5, GARBAGE)]
    await mountPage(HistoryPage)
    expect(cells()).toContain(GARBAGE)
    expect(pageText()).not.toContain('Invalid Date')
  })
})

describe('dashboard page', () => {
  it('renders last run, next run and source health times through parseUtc', async () => {
    status = {
      last_run: { id: 1, trigger: 'manual', status: 'success', started_at: ZONELESS, finished_at: null, summary: null, error: null },
      next_scheduled_run: ZULU,
      source_health: [{ source: 'slack_acme', status: 'ok', detail: 'fine', checked_at: '2026-09-21T13:30:00' }],
    }
    await mountPage(DashboardPage)
    const text = pageText()
    expect(text).toContain(`Next scheduled run: ${parseUtc(ZULU).toLocaleString()}`)
    expect(text).toContain(parseUtc(ZONELESS).toLocaleString())
    expect(text).toContain(parseUtc('2026-09-21T13:30:00').toLocaleString())
    expect(text).not.toContain(new Date(ZONELESS).toLocaleString())
    expect(text).not.toContain(new Date('2026-09-21T13:30:00').toLocaleString())
  })

  it('renders an unparseable time as the raw text, never Invalid Date', async () => {
    status = {
      last_run: { id: 1, trigger: 'manual', status: 'success', started_at: GARBAGE, finished_at: null, summary: null, error: null },
      next_scheduled_run: null,
      source_health: [{ source: 'slack_acme', status: 'ok', detail: 'fine', checked_at: 'never checked' }],
    }
    await mountPage(DashboardPage)
    const text = pageText()
    expect(text).toContain(GARBAGE)
    expect(text).toContain('never checked')
    expect(text).not.toContain('Invalid Date')
  })
})
