import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import CollectorsPage from '~/pages/collectors.vue'
import type { CollectorState, DigestConfig, DryRunJob, DryRunResult } from '~/composables/useApi'

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString().replace(/\.\d+Z$/, 'Z')

function collector(overrides: Partial<CollectorState> = {}): CollectorState {
  return {
    source: 'zoom',
    active: false,
    watermark: ago(5),
    consecutive_failures: 0,
    stuck_threshold: 3,
    last_attempt: { at: ago(5), status: 'ok', detail: null, item_count: 4 },
    item_count: 42,
    ...overrides,
  }
}

const DRY_RESULT: DryRunResult = {
  source: 'zoom',
  status: 'ok',
  detail: 'collected 3 items',
  window: { since: ago(48 * 60), until: ago(0), hours: 48 },
  count: 3,
  by_type: { message: 2, recording: 1 },
  items: [
    { item_type: 'message', occurred_at: ago(30), external_id: 'a', preview: 'first preview line' },
    { item_type: 'recording', occurred_at: ago(40), external_id: 'b', preview: 'second preview line' },
  ],
  duration_ms: 812,
}

interface Call {
  method: string
  path: string
  headers?: Record<string, string>
  body?: unknown
  params?: Record<string, unknown>
}

interface State {
  collectors: CollectorState[] | Error
  config: DigestConfig
  putConfig?: (body: DigestConfig) => DigestConfig | Error
  run: { accepted: boolean } | Error
  startDryRun: { job_id: string } | Error
  dryRun: () => DryRunJob | Error
  reset: (body: { days_back: number | null }) => unknown
  clear: () => unknown
}

let calls: Call[]
let state: State

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function freshState(): State {
  return {
    collectors: [],
    config: { schedule_cron: '0 6 * * *', tracker: 'none', active_sources: {} },
    run: { accepted: true },
    startDryRun: { job_id: 'job-1' },
    dryRun: () => ({ status: 'done', result: DRY_RESULT }),
    reset: (body) => ({ source: 'zoom', watermark: null, note: `API note for ${JSON.stringify(body)}` }),
    clear: () => ({ source: 'zoom', consecutive_failures: 0 }),
  }
}

/** A stand-in for the network: routes by method and path, records every call. */
function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; headers?: Record<string, string>; body?: string; params?: Record<string, unknown> } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      const body = options.body ? JSON.parse(options.body) : undefined
      calls.push({ method, path, headers: options.headers, body, params: options.params })
      const answer = (value: unknown) => {
        if (value instanceof Error) throw value
        return value
      }
      if (method === 'GET' && path === '/api/collectors') return answer(state.collectors)
      if (method === 'GET' && path === '/api/config') return state.config
      if (method === 'PUT' && path === '/api/config') return answer(state.putConfig ? state.putConfig(body) : body)
      if (method === 'GET' && path.startsWith('/api/collectors/dry-run/')) return answer(state.dryRun())
      if (method === 'POST' && path.endsWith('/run')) return answer(state.run)
      if (method === 'POST' && path.endsWith('/dry-run')) return answer(state.startDryRun)
      if (method === 'POST' && path.endsWith('/reset')) return answer(state.reset(body))
      if (method === 'POST' && path.endsWith('/clear-failures')) return answer(state.clear())
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

async function type(testid: string, value: string) {
  const el = $<HTMLInputElement>(testid)
  if (!el) throw new Error(`no input ${testid}`)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

function buttonWithLabel(label: string): HTMLElement {
  const el = [...body.querySelectorAll<HTMLElement>('button')].find((b) => b.textContent?.trim() === label)
  if (!el) throw new Error(`no button labeled "${label}"`)
  return el
}

// QPage renders nothing outside a QLayout, so the page is mounted in the same shell app.vue gives it.
const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(CollectorsPage))),
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
const rows = () => $$('collector')
const rowOf = (source: string) => {
  const row = body.querySelector<HTMLElement>(`[data-testid="collector"][data-source="${source}"]`)
  if (!row) throw new Error(`no row ${source}`)
  return row
}
const inRow = (source: string, testid: string) => {
  const el = rowOf(source).querySelector<HTMLElement>(`[data-testid="${testid}"]`)
  if (!el) throw new Error(`no ${testid} in ${source}`)
  return el
}
const callsTo = (method: string, suffix: string) => calls.filter((call) => call.method === method && call.path.endsWith(suffix))
const listCalls = () => calls.filter((call) => call.method === 'GET' && call.path === '/api/collectors')

/** Runs a source via its confirm dialog: click Run now, then confirm in the dialog that pops up. */
async function runNow(source: string) {
  inRow(source, 'collector-run').click()
  await flushPromises()
  buttonWithLabel('Run now').click()
  await flushPromises()
}

/** Clears failures via its confirm dialog: open the overflow menu, click Clear failures, then confirm. */
async function clearFailures(source: string) {
  inRow(source, 'collector-menu').click()
  await flushPromises()
  ;($(`action-clear-${source}`))?.click()
  await flushPromises()
  buttonWithLabel('Clear failures').click()
  await flushPromises()
}

beforeEach(() => {
  state = freshState()
  state.collectors = [collector()]
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('collectors page', () => {
  it('renders a row per collector with progress, failures, last run and item count', async () => {
    const watermark = ago(5)
    state.collectors = [
      collector({ watermark }),
      collector({
        source: 'slack_acme',
        consecutive_failures: 2,
        last_attempt: { at: ago(120), status: 'error', detail: 'Slack rejected the token', item_count: 0 },
        item_count: 0,
      }),
      collector({ source: 'gmail_work', watermark: null, last_attempt: null, item_count: 0 }),
    ]
    await mountPage()

    expect(rows()).toHaveLength(3)
    expect(inRow('zoom', 'collector-source').textContent?.trim()).toBe('Zoom')
    expect(inRow('zoom', 'collector-watermark').textContent).toContain('Collected up to')
    expect(inRow('zoom', 'collector-status').textContent).toContain('OK')
    expect(inRow('zoom', 'collector-items').textContent).toContain('42 items collected')
    expect(rowOf('zoom').querySelector('[data-testid="streak-warning"]')).toBeNull()

    expect(inRow('slack_acme', 'collector-source').textContent?.trim()).toBe('Slack acme')
    expect(inRow('slack_acme', 'streak-warning').textContent).toContain('Failing: 2 of 3 attempts before pause')
    expect(inRow('slack_acme', 'collector-status').textContent).toContain('Error')
    expect(inRow('slack_acme', 'attempt-detail').textContent).toBe('Slack rejected the token')

    expect(inRow('gmail_work', 'collector-watermark').textContent).toContain('Nothing collected yet')
    expect(inRow('gmail_work', 'collector-attempt').textContent).toContain('No runs yet')
    // Nothing to clear: the menu action is disabled at a zero streak.
    await click(`collector-menu`)
    expect(rowOf('gmail_work'))
  })

  it('shows the failing chip whenever there is at least one failure, however far from the threshold', async () => {
    state.collectors = [
      collector({ source: 'a', consecutive_failures: 1, stuck_threshold: 3 }),
      collector({ source: 'b', consecutive_failures: 0, stuck_threshold: 3 }),
    ]
    await mountPage()
    expect(inRow('a', 'streak-warning').textContent).toContain('Failing: 1 of 3 attempts before pause')
    expect(rowOf('b').querySelector('[data-testid="streak-warning"]')).toBeNull()
  })

  it('renders server text as text, never as markup', async () => {
    state.collectors = [
      collector({ last_attempt: { at: ago(1), status: 'error', detail: '<img src=x onerror=alert(1)><b>bold</b>', item_count: 0 } }),
    ]
    await mountPage()
    const attempt = inRow('zoom', 'collector-attempt')
    expect(attempt.textContent).toContain('<img src=x onerror=alert(1)><b>bold</b>')
    expect(attempt.querySelector('img')).toBeNull()
    expect(attempt.querySelector('b')).toBeNull()
  })

  it('shows the empty state', async () => {
    state.collectors = []
    await mountPage()
    expect($('empty-state')?.textContent).toContain('No collectors are declared')
  })

  it('shows a loading state until the request settles', async () => {
    let release: (value: CollectorState[]) => void = () => {}
    const gate = new Promise<CollectorState[]>((resolve) => (release = resolve))
    vi.stubGlobal('$fetch', async () => gate)
    await mountPage()
    expect($('loading')).not.toBeNull()
    expect($('empty-state')).toBeNull()

    release([collector()])
    await flushPromises()
    expect($('loading')).toBeNull()
    expect(rows()).toHaveLength(1)
  })

  it('shows the API error text and retries', async () => {
    state.collectors = apiFailure(500, 'The store is unavailable')
    await mountPage()
    expect($('load-error')?.textContent).toContain('The store is unavailable')
    expect($('empty-state')).toBeNull()

    state.collectors = [collector()]
    await click('retry')
    expect($('load-error')).toBeNull()
    expect(rows()).toHaveLength(1)
  })

  it('the active toggle reads the config, writes the merged active_sources and keeps the new state', async () => {
    state.config = { schedule_cron: '0 7 * * *', tracker: 'jira', active_sources: { slack_acme: true, zoom: false } }
    await mountPage()
    const toggle = () => inRow('zoom', 'collector-active')
    expect(toggle().getAttribute('aria-checked')).toBe('false')

    toggle().click()
    await flushPromises()

    expect(calls.filter((call) => call.path === '/api/config').map((call) => call.method)).toEqual(['GET', 'PUT'])
    const put = calls.find((call) => call.method === 'PUT')
    expect(put?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(put?.body).toEqual({ schedule_cron: '0 7 * * *', tracker: 'jira', active_sources: { slack_acme: true, zoom: true } })
    expect(toggle().getAttribute('aria-checked')).toBe('true')
    expect(rowOf('zoom').querySelector('[data-testid="toggle-error"]')).toBeNull()
  })

  it('the active toggle reverts and shows the error when the save fails', async () => {
    state.collectors = [collector({ active: true })]
    state.putConfig = () => apiFailure(422, 'active_sources: unknown source')
    await mountPage()
    const toggle = () => inRow('zoom', 'collector-active')
    expect(toggle().getAttribute('aria-checked')).toBe('true')

    toggle().click()
    await flushPromises()

    expect(toggle().getAttribute('aria-checked')).toBe('true')
    expect(inRow('zoom', 'toggle-error').textContent?.trim()).toBe('active_sources: unknown source')
    await vi.waitFor(() => expect(body.textContent).toContain('active_sources: unknown source'))
  })
})

describe('run now', () => {
  it('asks to confirm before starting, and a 409 shows that a run is already in progress', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    state.run = apiFailure(409, { code: 'run_in_progress', message: 'A run is already in progress' })
    await mountPage()
    const timers = vi.getTimerCount()

    inRow('zoom', 'collector-run').click()
    await flushPromises()
    expect(body.textContent).toContain('Collect from Zoom now?')
    expect(callsTo('POST', '/collectors/zoom/run')).toHaveLength(0)

    buttonWithLabel('Run now').click()
    await flushPromises()

    const post = callsTo('POST', '/collectors/zoom/run')[0]
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    await vi.waitFor(() => expect(body.textContent).toContain('A run is already in progress'))
    expect(isLoading(inRow('zoom', 'collector-run'))).toBe(false)
    expect(vi.getTimerCount()).toBe(timers)
  })

  it('cancelling the confirm starts nothing', async () => {
    await mountPage()
    inRow('zoom', 'collector-run').click()
    await flushPromises()
    buttonWithLabel('Cancel').click()
    await flushPromises()
    expect(callsTo('POST', '/collectors/zoom/run')).toHaveLength(0)
  })

  it('polls the list until last_attempt.at changes, then stops', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    await mountPage()
    const timers = vi.getTimerCount()

    await runNow('zoom')
    expect(vi.getTimerCount()).toBeGreaterThan(timers)
    expect(isLoading(inRow('zoom', 'collector-run'))).toBe(true)

    const initial = listCalls().length
    await vi.advanceTimersByTimeAsync(3_100)
    expect(listCalls().length - initial).toBe(1)
    // Unchanged last_attempt.at: still running.
    expect(isLoading(inRow('zoom', 'collector-run'))).toBe(true)

    state.collectors = [collector({ last_attempt: { at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), status: 'ok', detail: null, item_count: 9 }, item_count: 51 })]
    await vi.advanceTimersByTimeAsync(3_100)
    await flushPromises()

    expect(isLoading(inRow('zoom', 'collector-run'))).toBe(false)
    expect(inRow('zoom', 'collector-items').textContent).toContain('51 items collected')
    await vi.waitFor(() => expect(body.textContent).toContain('Zoom: run finished (ok)'))
    expect(vi.getTimerCount()).toBe(timers)
    const settled = listCalls().length
    await vi.advanceTimersByTimeAsync(20_000)
    expect(listCalls().length).toBe(settled)
  })

  it('clears the poll timer on unmount', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    const wrapper = await mountPage()
    const timers = vi.getTimerCount()
    await runNow('zoom')
    expect(vi.getTimerCount()).toBeGreaterThan(timers)

    wrapper.unmount()
    mounted = mounted.filter((entry) => entry !== wrapper)
    expect(vi.getTimerCount()).toBe(timers)
  })
})

describe('dry run', () => {
  async function openDryRun() {
    await mountPage()
    await click('collector-menu')
    ;(($(`action-dry-run-zoom`)) as HTMLElement).click()
    await vi.waitFor(() => expect($('dry-dialog')).not.toBeNull())
  }

  it('sends hours and limit, polls the job and shows counts by type and previews', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    let polls = 0
    state.dryRun = () => (++polls < 3 ? { status: 'running', result: null } : { status: 'done', result: DRY_RESULT })
    await openDryRun()
    const timers = vi.getTimerCount()
    expect($<HTMLInputElement>('dry-hours')?.value).toBe('24')
    expect($<HTMLInputElement>('dry-limit')?.value).toBe('5')

    await type('dry-hours', '48')
    await type('dry-limit', '3')
    await click('dry-start')

    const post = callsTo('POST', '/collectors/zoom/dry-run')[0]
    expect(post?.body).toEqual({ hours: 48, limit: 3 })
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    expect($('dry-running')).not.toBeNull()
    expect(body.textContent).not.toContain('checks every 2 seconds')
    expect(vi.getTimerCount()).toBeGreaterThan(timers)

    await vi.advanceTimersByTimeAsync(2_100)
    await vi.advanceTimersByTimeAsync(2_100)
    expect($('dry-running')).not.toBeNull()
    expect(calls.filter((call) => call.path === '/api/collectors/dry-run/job-1')).toHaveLength(2)

    await vi.advanceTimersByTimeAsync(2_100)
    await flushPromises()
    expect($('dry-running')).toBeNull()
    expect($('dry-summary')?.textContent).toContain('OK')
    expect($('dry-summary')?.textContent).toContain('collected 3 items')
    expect($('dry-count')?.textContent).toContain('3 items')
    expect($$('dry-type').map((chip) => chip.textContent?.trim())).toEqual(['Message: 2', 'Recording: 1'])
    expect($$('dry-item').map((item) => item.textContent)).toEqual([
      expect.stringContaining('first preview line'),
      expect.stringContaining('second preview line'),
    ])
    expect($('dry-raw-toggle')).toBeNull()
    expect(vi.getTimerCount()).toBe(timers)
    // Terminal state: no more polling.
    const settled = calls.length
    await vi.advanceTimersByTimeAsync(20_000)
    expect(calls.length).toBe(settled)
  })

  it('offers Show raw only when the result carries raw and renders it as text', async () => {
    state.dryRun = () => ({
      status: 'done',
      result: { ...DRY_RESULT, raw: [{ body: '<b>raw</b>' }] } as DryRunResult,
    })
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    await openDryRun()
    await click('dry-start')
    await vi.advanceTimersByTimeAsync(2_100)
    await flushPromises()

    expect($('dry-raw')).toBeNull()
    await click('dry-raw-toggle')
    const raw = $('dry-raw')
    expect(raw?.tagName).toBe('PRE')
    expect(raw?.textContent).toContain('<b>raw</b>')
    expect(raw?.querySelector('b')).toBeNull()
  })

  it('shows a failed job without a result and lets the user run again', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    state.dryRun = () => ({ status: 'failed', result: null })
    await openDryRun()
    const timers = vi.getTimerCount()
    await click('dry-start')
    await vi.advanceTimersByTimeAsync(2_100)
    await flushPromises()

    expect($('dry-error')?.textContent).toContain('The dry run failed')
    expect(vi.getTimerCount()).toBe(timers)
    await click('dry-again')
    expect($('dry-start')).not.toBeNull()
    expect($('dry-error')).toBeNull()
  })

  it('shows the 429 message and stays on the form', async () => {
    state.startDryRun = apiFailure(429, { code: 'too_many_dry_runs', message: 'Too many dry runs are in progress; try again shortly' })
    await openDryRun()
    await click('dry-start')
    expect($('dry-error')?.textContent).toContain('Too many dry runs are in progress')
    expect($('dry-start')).not.toBeNull()
    expect($('dry-running')).toBeNull()
  })

  it('does not start with values outside the API limits', async () => {
    await openDryRun()
    await type('dry-hours', '169')
    expect($('dry-start')?.hasAttribute('disabled')).toBe(true)
    await type('dry-hours', '168')
    await type('dry-limit', '51')
    expect($('dry-start')?.hasAttribute('disabled')).toBe(true)
    await type('dry-limit', '0')
    expect($('dry-start')?.hasAttribute('disabled')).toBe(false)
  })

  it('stops polling when the dialog is closed', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    state.dryRun = () => ({ status: 'running', result: null })
    await openDryRun()
    const timers = vi.getTimerCount()
    await click('dry-start')
    expect(vi.getTimerCount()).toBeGreaterThan(timers)

    await click('dry-close')
    expect(vi.getTimerCount()).toBe(timers)
    const settled = calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(calls.length).toBe(settled)
  })
})

describe('start over (reset watermark) and clear failures', () => {
  async function openReset() {
    await mountPage()
    await click('collector-menu')
    ;(($(`action-reset-zoom`)) as HTMLElement).click()
    await vi.waitFor(() => expect($('reset-dialog')).not.toBeNull())
  }

  it('sends the chosen days_back and then shows the note from the response', async () => {
    await openReset()
    await click('reset-note-toggle')
    expect($('reset-note-static')?.textContent).toContain('never further back than 7 days')
    expect($('reset-note')).toBeNull()

    await click('reset-choice-3')
    await click('reset-confirm')

    const post = callsTo('POST', '/collectors/zoom/reset')[0]
    expect(post?.body).toEqual({ days_back: 3 })
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    expect($('reset-note')?.textContent).toBe('API note for {"days_back":3}')
    expect($('reset-result')?.textContent).toContain('Collecting will start from scratch next run.')
    // The list is refreshed in place.
    expect(listCalls().length).toBe(2)
  })

  it('defaults to one day back', async () => {
    await openReset()
    await click('reset-confirm')
    expect(callsTo('POST', '/collectors/zoom/reset')[0]?.body).toEqual({ days_back: 1 })
  })

  it('From scratch sends days_back null and the response starting point is shown', async () => {
    state.reset = (payload) => ({ source: 'zoom', watermark: payload.days_back === null ? null : ago(1), note: 'Removed. ' + 'Note text' })
    await openReset()
    await click('reset-choice-none')
    await click('reset-confirm')
    expect(callsTo('POST', '/collectors/zoom/reset')[0]?.body).toEqual({ days_back: null })
    expect($('reset-result')?.textContent).toContain('Collecting will start from scratch next run.')
    expect($('reset-note')?.textContent).toBe('Removed. Note text')
  })

  it('shows the API error text and keeps the dialog open', async () => {
    state.reset = () => apiFailure(404, { code: 'unknown_source', message: 'Unknown source' })
    await openReset()
    await click('reset-confirm')
    expect($('reset-error')?.textContent).toContain('Unknown source')
    expect($('reset-confirm')).not.toBeNull()
  })

  it('Clear failures asks to confirm, then posts and refreshes the streak', async () => {
    state.collectors = [collector({ consecutive_failures: 2 })]
    await mountPage()
    expect(inRow('zoom', 'streak-warning').textContent).toContain('Failing: 2 of 3 attempts before pause')

    state.collectors = [collector({ consecutive_failures: 0 })]
    await clearFailures('zoom')

    const post = callsTo('POST', '/collectors/zoom/clear-failures')[0]
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(rowOf('zoom').querySelector('[data-testid="streak-warning"]')).toBeNull()
  })

  it('the Items button links to the source items route', async () => {
    await mountPage()
    const link = inRow('zoom', 'collector-browse') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/collectors/zoom')
  })
})

