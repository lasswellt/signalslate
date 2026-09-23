import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import HistoryDetailPage from '~/pages/history/[id].vue'
import { parseUtc } from '~/composables/useApi'
import type { RunDetail } from '~/composables/useApi'
import { formatDate } from '~/utils/format'

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

function runDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 7,
    trigger: 'manual',
    status: 'success',
    started_at: ZULU,
    finished_at: null,
    summary: 'summary 7',
    error: null,
    has_pdf: false,
    source_health: [{ source: 'slack_acme', status: 'ok', detail: 'fine', checked_at: ZONELESS }],
    ...overrides,
  }
}

let detail: RunDetail | Error

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

beforeEach(() => {
  detail = runDetail()
  vi.stubGlobal('$fetch', async (url: string) => {
    const path = new URL(url).pathname
    if (path === '/api/runs/7') {
      if (detail instanceof Error) throw detail
      return detail
    }
    throw new Error(`unexpected GET ${path}`)
  })
})

let mounted: Array<VueWrapper<unknown>> = []

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

// QPage renders nothing outside a QLayout, so the page is mounted in the same shell app.vue gives it.
// The route option makes useRoute() see the right path without stubbing the composable.
async function mountPage(path = '/history/7') {
  const Harness = defineComponent({
    render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(HistoryDetailPage))),
  })
  const wrapper = await mountSuspended(Harness, { attachTo: document.body, route: path })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)
const pageText = () => body.textContent ?? ''

describe('test setup', () => {
  it('runs in a non-UTC zone where a zone-less string parsed as local time is a different instant', () => {
    expect(new Date(ZONELESS).getTimezoneOffset()).not.toBe(0)
    expect(parseUtc(ZONELESS).toLocaleString()).not.toBe(new Date(ZONELESS).toLocaleString())
  })
})

describe('loading, error and not-found', () => {
  it('shows a loading skeleton while the request is in flight', async () => {
    let release: () => void = () => {}
    const gate = new Promise<RunDetail>((resolve) => {
      release = () => resolve(runDetail())
    })
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/runs/7') return gate
      throw new Error(`unexpected ${path}`)
    })
    await mountPage()
    expect($('loading')).not.toBeNull()

    release()
    await flushPromises()
    expect($('loading')).toBeNull()
    expect(pageText()).toContain('summary 7')
  })

  it('shows the API error and retries', async () => {
    detail = apiFailure(500, 'The store is unavailable')
    await mountPage()
    expect($('load-error')?.textContent).toContain('The store is unavailable')

    detail = runDetail()
    $('load-error')?.querySelector('button')?.click()
    await flushPromises()
    expect($('load-error')).toBeNull()
    expect(pageText()).toContain('summary 7')
  })

  it('shows "Run not found" on a 404 from the API, with a link back to Runs', async () => {
    detail = apiFailure(404, 'Run not found')
    await mountPage()
    expect($('empty-state')).not.toBeNull()
    expect(pageText()).toContain('Run not found')
    expect($('run-not-found-back')?.getAttribute('href')).toBe('/history')
  })

  it('shows "Run not found" for a non-numeric id, without calling the API', async () => {
    await mountPage('/history/not-a-number')
    expect($('empty-state')).not.toBeNull()
    expect(pageText()).toContain('Run not found')
  })
})

describe('run detail', () => {
  it('renders the page title from the run start time and the summary', async () => {
    detail = runDetail({ started_at: ZULU })
    await mountPage()
    expect(pageText()).toContain(`Run on ${formatDate(ZULU)}`)
    expect(pageText()).toContain('summary 7')
  })

  it('renders a Z-suffixed started_at as the instant parseUtc gives', async () => {
    detail = runDetail({ started_at: ZULU })
    await mountPage()
    expect(pageText()).toContain(formatDate(ZULU))
  })

  it('treats a zone-less started_at as UTC, not as viewer-local time', async () => {
    detail = runDetail({ started_at: ZONELESS })
    await mountPage()
    expect(pageText()).toContain(formatDate(ZONELESS))
    expect(pageText()).not.toContain(new Date(ZONELESS).toLocaleString())
  })

  it('renders an unparseable started_at via the shared fallback, never Invalid Date', async () => {
    detail = runDetail({ started_at: GARBAGE })
    await mountPage()
    expect(pageText()).not.toContain('Invalid Date')
  })

  it('shows duration computed from started/finished', async () => {
    detail = runDetail({ started_at: ZULU, finished_at: '2026-09-21T12:00:05Z' })
    await mountPage()
    expect($('run-duration')?.textContent).toContain('5 s')
  })

  it('shows a dash for duration while the run has not finished', async () => {
    detail = runDetail({ finished_at: null })
    await mountPage()
    expect($('run-duration')?.textContent).toContain('—')
  })

  it('shows the error banner when the run has one', async () => {
    detail = runDetail({ error: 'Collector timed out' })
    await mountPage()
    expect($('run-error')?.textContent).toContain('Collector timed out')
  })

  it('shows the Download PDF button only when has_pdf is true', async () => {
    detail = runDetail({ has_pdf: false })
    await mountPage()
    expect($('download-pdf')).toBeNull()

    detail = runDetail({ has_pdf: true })
    await mountPage()
    expect($('download-pdf')).not.toBeNull()
  })

  it('renders source health via HealthList', async () => {
    detail = runDetail({ source_health: [{ source: 'slack_acme', status: 'ok', detail: 'fine', checked_at: ZONELESS }] })
    await mountPage()
    expect($('health-list')).not.toBeNull()
    expect(pageText()).toContain('Slack acme')
  })

  it('shows the HealthList empty state when a run has no source health', async () => {
    detail = runDetail({ source_health: [] })
    await mountPage()
    expect($('health-empty')).not.toBeNull()
  })
})
