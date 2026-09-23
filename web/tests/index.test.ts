import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import IndexPage from '~/pages/index.vue'
import type { CollectorState, ConnectionView, StatusResponse } from '~/composables/useApi'

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString().replace(/\.\d+Z$/, 'Z')
const inFuture = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString().replace(/\.\d+Z$/, 'Z')

function statusResponse(overrides: Partial<StatusResponse> = {}): StatusResponse {
  return {
    last_run: {
      id: 42,
      trigger: 'scheduled',
      status: 'success',
      started_at: ago(30),
      finished_at: ago(29),
      summary: 'Collected 12 items',
      error: null,
    },
    next_scheduled_run: inFuture(130),
    source_health: [
      { source: 'zoom', status: 'ok', detail: 'Connected', checked_at: ago(10) },
      { source: 'slack_acme', status: 'error', detail: 'Token expired', checked_at: ago(120) },
    ],
    ...overrides,
  }
}

function connection(overrides: Partial<ConnectionView> = {}): ConnectionView {
  return {
    id: 'zoom-1',
    kind: 'zoom',
    label: 'Zoom',
    origin: 'ui',
    config: {},
    secrets_set: [],
    active: true,
    health: { status: 'ok', detail: null, checked_at: ago(10) },
    ...overrides,
  }
}

function collector(overrides: Partial<CollectorState> = {}): CollectorState {
  return {
    source: 'zoom',
    active: true,
    watermark: ago(5),
    consecutive_failures: 0,
    stuck_threshold: 3,
    last_attempt: { at: ago(5), status: 'ok', detail: null, item_count: 4 },
    item_count: 42,
    ...overrides,
  }
}

interface Call {
  method: string
  path: string
  headers?: Record<string, string>
}

interface State {
  status: StatusResponse | Error
  connections: ConnectionView[] | Error
  collectors: CollectorState[] | Error
  trigger: { accepted: boolean } | Error
}

let calls: Call[]
let state: State

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function freshState(): State {
  return {
    status: statusResponse(),
    connections: [connection()],
    collectors: [collector()],
    trigger: { accepted: true },
  }
}

/** A stand-in for the network: routes by method and path, records every call. */
function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; headers?: Record<string, string> } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, headers: options.headers })
      const answer = (value: unknown) => {
        if (value instanceof Error) throw value
        return value
      }
      if (method === 'GET' && path === '/api/status') return answer(state.status)
      if (method === 'GET' && path === '/api/connections') return answer(state.connections)
      if (method === 'GET' && path === '/api/collectors') return answer(state.collectors)
      if (method === 'POST' && path === '/api/runs/trigger') return answer(state.trigger)
      throw new Error(`unexpected ${method} ${path}`)
    },
  )
}

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)
const $$ = (testid: string) => [...body.querySelectorAll<HTMLElement>(`[data-testid="${testid}"]`)]

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

// QPage renders nothing outside a QLayout, so the page is mounted in the same shell app.vue gives it.
const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(IndexPage))),
})

let mounted: Array<VueWrapper<unknown>> = []

async function mountPage() {
  const wrapper = await mountSuspended(Harness, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

// QBtn marks `loading` with a spinner in its content, not with a class on the button.
const isLoading = (el: HTMLElement) => el.querySelector('.q-spinner') !== null

beforeEach(() => {
  state = freshState()
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('loading, empty and error', () => {
  it('shows a loading skeleton, not an empty or KPI state, while the request is in flight', async () => {
    let release: () => void = () => {}
    const gate = new Promise<StatusResponse>((resolve) => {
      release = () => resolve(statusResponse())
    })
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/status') return gate
      if (path === '/api/connections') return []
      if (path === '/api/collectors') return []
      throw new Error(`unexpected ${path}`)
    })

    await mountPage()
    expect($('loading')).not.toBeNull()
    expect($('kpi-row')).toBeNull()
    expect($('health-empty')).toBeNull()

    release()
    await flushPromises()
    expect($('loading')).toBeNull()
    expect($('kpi-row')).not.toBeNull()
  })

  it('shows the API error text and retries', async () => {
    state.status = apiFailure(500, 'The store is unavailable')
    await mountPage()
    expect($('load-error')?.textContent).toContain('The store is unavailable')
    expect($('kpi-row')).toBeNull()

    state.status = statusResponse()
    $('load-error')?.querySelector('button')?.click()
    await flushPromises()
    expect($('load-error')).toBeNull()
    expect($('kpi-row')).not.toBeNull()
  })

  it('degrades a KPI tile to Unavailable when a secondary fetch fails, without failing the page', async () => {
    state.connections = apiFailure(500, 'boom')
    await mountPage()
    expect($('load-error')).toBeNull()
    expect($('kpi-accounts-unavailable')?.textContent?.trim()).toBe('Unavailable')
    expect($('kpi-collectors-value')?.textContent).toContain('0 failing')
  })
})

describe('KPI values', () => {
  it('shows the last run status + relative time linking to its history detail', async () => {
    await mountPage()
    const link = $('kpi-last-run-link')
    expect(link?.getAttribute('href')).toBe('/history/42')
    expect(link?.querySelector('.q-chip__content')?.textContent?.trim()).toBe('Success')
    expect(link?.textContent).toContain('30 min ago')
  })

  it('shows "No runs yet" when there is no last run', async () => {
    state.status = statusResponse({ last_run: null })
    await mountPage()
    expect($('kpi-last-run-empty')?.textContent).toBe('No runs yet')
    expect($('kpi-last-run-link')).toBeNull()
  })

  it('shows the next scheduled run date and relative time', async () => {
    await mountPage()
    expect($('kpi-next-run-value')?.textContent).toContain('in 2 h')
  })

  it('shows "Not scheduled" when there is no next run', async () => {
    state.status = statusResponse({ next_scheduled_run: null })
    await mountPage()
    expect($('kpi-next-run-empty')?.textContent?.trim()).toBe('Not scheduled')
  })

  it('counts healthy vs total accounts from the connections list', async () => {
    state.connections = [
      connection({ id: 'zoom-1', health: { status: 'ok', detail: null, checked_at: ago(1) } }),
      connection({ id: 'slack-1', health: { status: 'error', detail: 'bad token', checked_at: ago(1) } }),
      connection({ id: 'gmail-1', health: null }),
    ]
    await mountPage()
    expect($('kpi-accounts-value')?.textContent).toContain('1 healthy / 3 total')
  })

  it('counts failing collectors from the collectors list', async () => {
    state.collectors = [
      collector({ source: 'zoom', consecutive_failures: 0 }),
      collector({ source: 'slack_acme', consecutive_failures: 3 }),
      collector({ source: 'gmail_work', consecutive_failures: 1 }),
    ]
    await mountPage()
    expect($('kpi-collectors-value')?.textContent).toContain('2 failing')
  })
})

describe('source health', () => {
  it('renders a HealthList row per source with status, name, detail and checked time', async () => {
    await mountPage()
    const rows = $$('health-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]?.querySelector('[data-testid="health-source"]')?.textContent).toBe('Zoom')
    expect(rows[0]?.querySelector('[data-testid="health-detail"]')?.textContent).toBe('Connected')
    expect(rows[0]?.querySelector('[data-testid="health-checked"]')?.textContent).toContain('10 min ago')
    expect(rows[0]?.getAttribute('href')).toBe('/collectors')
  })

  it('shows the empty state when there is no source health yet', async () => {
    state.status = statusResponse({ source_health: [] })
    await mountPage()
    expect($('health-empty')).not.toBeNull()
    expect($$('health-row')).toHaveLength(0)
  })
})

describe('run digest now', () => {
  it('shows the trigger error and stops the spinner without polling', async () => {
    state.trigger = apiFailure(500, 'Could not start the run')
    await mountPage()

    await click('trigger-run')

    expect(isLoading($('trigger-run')!)).toBe(false)
    await vi.waitFor(() => expect(body.textContent).toContain('Could not start the run'))
  })

  it('polls until a run other than the previous one appears and has finished, then stops', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    state.status = statusResponse({
      last_run: { id: 1, trigger: 'scheduled', status: 'success', started_at: ago(60), finished_at: ago(59), summary: null, error: null },
    })
    await mountPage()
    const timers = vi.getTimerCount()

    $('trigger-run')?.click()
    await flushPromises()
    expect(vi.getTimerCount()).toBeGreaterThan(timers)
    expect(isLoading($('trigger-run')!)).toBe(true)

    // A new run has landed but is still running: keep polling.
    state.status = statusResponse({
      last_run: { id: 2, trigger: 'manual', status: 'running', started_at: ago(0), finished_at: null, summary: null, error: null },
    })
    await vi.advanceTimersByTimeAsync(3_100)
    expect(isLoading($('trigger-run')!)).toBe(true)

    // The new run finishes: stop.
    state.status = statusResponse({
      last_run: { id: 2, trigger: 'manual', status: 'success', started_at: ago(0), finished_at: ago(0), summary: 'ok', error: null },
    })
    await vi.advanceTimersByTimeAsync(3_100)
    await flushPromises()

    expect(isLoading($('trigger-run')!)).toBe(false)
    expect(vi.getTimerCount()).toBe(timers)
    const settled = calls.filter((call) => call.path === '/api/status').length
    await vi.advanceTimersByTimeAsync(20_000)
    expect(calls.filter((call) => call.path === '/api/status').length).toBe(settled)
  })

  it('does not stop for a run that keeps the same id even once it is no longer "running"', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    state.status = statusResponse({
      last_run: { id: 5, trigger: 'scheduled', status: 'success', started_at: ago(60), finished_at: ago(59), summary: null, error: null },
    })
    await mountPage()

    $('trigger-run')?.click()
    await flushPromises()
    expect(isLoading($('trigger-run')!)).toBe(true)

    // Stale: the run status API still reports the pre-trigger run (same id), even though it isn't "running".
    await vi.advanceTimersByTimeAsync(3_100)
    expect(isLoading($('trigger-run')!)).toBe(true)
  })

  it('stops after the cap and notifies when no new finished run ever appears', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    state.status = statusResponse({
      last_run: { id: 9, trigger: 'scheduled', status: 'running', started_at: ago(0), finished_at: null, summary: null, error: null },
    })
    await mountPage()

    $('trigger-run')?.click()
    await flushPromises()
    expect(isLoading($('trigger-run')!)).toBe(true)

    await vi.advanceTimersByTimeAsync(5 * 60_000 + 3_100)
    await flushPromises()

    expect(isLoading($('trigger-run')!)).toBe(false)
    await vi.waitFor(() => expect(body.textContent).toContain('taking longer than expected'))
  })

  it('clears the poll timer on unmount', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    const wrapper = await mountPage()
    const timers = vi.getTimerCount()
    $('trigger-run')?.click()
    await flushPromises()
    expect(vi.getTimerCount()).toBeGreaterThan(timers)

    wrapper.unmount()
    mounted = mounted.filter((entry) => entry !== wrapper)
    expect(vi.getTimerCount()).toBe(timers)
  })
})
