import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import HistoryPage from '~/pages/history.vue'
import { parseUtc } from '~/composables/useApi'
import type { RunSummary } from '~/composables/useApi'
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

function run(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 1,
    trigger: 'manual',
    status: 'success',
    started_at: ZULU,
    finished_at: null,
    summary: 'summary 1',
    error: null,
    has_pdf: false,
    ...overrides,
  }
}

let runs: RunSummary[]

beforeEach(() => {
  runs = []
  vi.stubGlobal('$fetch', async (url: string) => {
    const path = new URL(url).pathname
    if (path === '/api/runs') return runs
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
const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(HistoryPage))),
})

async function mountPage() {
  const wrapper = await mountSuspended(Harness, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)
const $$ = (testid: string) => [...body.querySelectorAll<HTMLElement>(`[data-testid="${testid}"]`)]
const pageText = () => body.textContent ?? ''

describe('test setup', () => {
  it('runs in a non-UTC zone where a zone-less string parsed as local time is a different instant', () => {
    expect(new Date(ZONELESS).getTimezoneOffset()).not.toBe(0)
    expect(parseUtc(ZONELESS).toLocaleString()).not.toBe(new Date(ZONELESS).toLocaleString())
  })
})

describe('loading, empty and error', () => {
  it('shows a loading skeleton while the request is in flight', async () => {
    let release: () => void = () => {}
    const gate = new Promise<RunSummary[]>((resolve) => {
      release = () => resolve([])
    })
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/runs') return gate
      throw new Error(`unexpected ${path}`)
    })

    await mountPage()
    expect($('loading')).not.toBeNull()
    expect($('runs-table')).toBeNull()

    release()
    await flushPromises()
    expect($('loading')).toBeNull()
  })

  it('shows "No runs yet" with a link to Overview when there are no runs', async () => {
    runs = []
    await mountPage()
    expect(pageText()).toContain('No runs yet')
    expect($('history-empty-cta')?.getAttribute('href')).toBe('/')
    expect($('runs-table')).toBeNull()
  })

  it('shows the API error and stops loading, never leaving the spinner stuck', async () => {
    vi.stubGlobal('$fetch', async () => {
      throw Object.assign(new Error('fetch failed'), { status: 500, data: { detail: 'The store is unavailable' } })
    })
    await mountPage()
    expect($('load-error')?.textContent).toContain('The store is unavailable')
    expect($('loading')).toBeNull()
  })
})

describe('runs table', () => {
  it('renders a Z-suffixed started_at as the instant parseUtc gives, via formatDate', async () => {
    runs = [run({ id: 1, started_at: ZULU })]
    await mountPage()
    expect(pageText()).toContain(formatDate(ZULU))
  })

  it('treats a zone-less started_at as UTC, not as viewer-local time', async () => {
    runs = [run({ id: 2, started_at: ZONELESS })]
    await mountPage()
    expect(pageText()).toContain(formatDate(ZONELESS))
    expect(pageText()).not.toContain(new Date(ZONELESS).toLocaleString())
  })

  it('renders an unparseable started_at as the shared fallback dash, never Invalid Date', async () => {
    runs = [run({ id: 5, started_at: GARBAGE })]
    await mountPage()
    expect(pageText()).toContain(formatDate(GARBAGE))
    expect(pageText()).not.toContain('Invalid Date')
  })

  it('shows status as a chip, humanized trigger, and duration from started/finished', async () => {
    runs = [
      run({ id: 10, trigger: 'manual-source', status: 'success', started_at: ZULU, finished_at: '2026-09-21T12:00:05Z' }),
    ]
    await mountPage()
    expect(pageText()).toContain('Single source')
    expect(pageText()).toContain('Success')
    expect(pageText()).toContain('5 s')
  })

  it('shows a dash for duration while a run has not finished', async () => {
    runs = [run({ id: 11, finished_at: null })]
    await mountPage()
    const row = $$('run-row')[0]
    expect(row?.textContent).toContain('—')
  })

  it('shows the PDF button only when has_pdf is true, labelled for the run', async () => {
    runs = [run({ id: 12, has_pdf: true, started_at: ZULU })]
    await mountPage()
    const btn = $('run-pdf')
    expect(btn).not.toBeNull()
    expect(btn?.getAttribute('aria-label')).toBe(`Download PDF for run of ${formatDate(ZULU)}`)
    expect(btn?.getAttribute('href')).toMatch(/\/api\/runs\/12\/pdf$/)
  })

  it('navigates to the run detail on row click', async () => {
    runs = [run({ id: 42 })]
    const wrapper = await mountPage()
    const router = (wrapper.vm as unknown as { $router: { currentRoute: { value: { fullPath: string } } } }).$router
    await wrapper.find('[data-testid="run-row"]').trigger('click')
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/history/42'))
  })

  it('navigates to the run detail on Enter when the row is focused', async () => {
    runs = [run({ id: 43 })]
    const wrapper = await mountPage()
    const router = (wrapper.vm as unknown as { $router: { currentRoute: { value: { fullPath: string } } } }).$router
    await wrapper.find('[data-testid="run-row"]').trigger('keyup.enter')
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/history/43'))
  })
})
