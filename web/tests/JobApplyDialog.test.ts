import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import JobApplyDialog from '~/components/JobApplyDialog.vue'
import type { ApplicationOut } from '~/composables/useJobsApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function application(overrides: Partial<ApplicationOut> = {}): ApplicationOut {
  return {
    id: 1,
    posting_id: 1,
    status: 'ready',
    packet: {
      cover_letter_text: 'Dear hiring team, ...',
      screening_drafts: [{ question: 'Why us?', answer: 'Because.', source: 'generated' }],
    },
    cover_letter_path: '/tmp/cover.pdf',
    resume_path: null,
    assist_state: 'idle',
    assist_session_id: null,
    created_at: new Date().toISOString(),
    submitted_at: null,
    ...overrides,
  }
}

let packetResult: () => unknown
let editResult: () => unknown
let queueResult: () => unknown
let listResult: () => unknown
let statusResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'POST' && path === '/api/jobs/applications/1/packet') {
        const result = packetResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'PUT' && path === '/api/jobs/applications/1/packet') {
        const result = editResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/applications/1/assist') {
        const result = queueResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'GET' && path === '/api/jobs/applications') {
        const result = listResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'PATCH' && path === '/api/jobs/applications/1') {
        const result = statusResult()
        if (result instanceof Error) throw result
        return result
      }
      throw new Error(`unexpected ${method} ${path}`)
    },
  )
}

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

async function setInput(testid: string, value: string) {
  const el = $<HTMLInputElement | HTMLTextAreaElement>(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountDialog(props: { applicationId?: number } = {}) {
  const wrapper = await mountSuspended(JobApplyDialog, {
    props: { open: true, applicationId: props.applicationId ?? 1 },
  })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  packetResult = () => application()
  editResult = () => application({ packet: { cover_letter_text: 'Edited text', screening_drafts: [] } })
  queueResult = () => application({ status: 'ready', assist_state: 'queued' })
  listResult = () => [application({ status: 'ready', assist_state: 'queued' })]
  statusResult = () => application({ status: 'submitted', submitted_at: new Date().toISOString() })
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('preparing the packet', () => {
  it('shows a loading state before the packet resolves, then renders it', async () => {
    let release: (value: unknown) => void = () => undefined
    packetResult = () => new Promise((resolve) => { release = resolve }) as unknown
    const wrapper = await mountSuspended(JobApplyDialog, { props: { open: true, applicationId: 1 } })
    mounted.push(wrapper as VueWrapper<unknown>)
    expect($('job-packet-loading')).not.toBeNull()
    release(application())
    await flushPromises()
    expect($('job-packet-loading')).toBeNull()
    expect($<HTMLTextAreaElement>('job-cover-letter')?.value).toContain('Dear hiring team')
  })

  it('shows a retry action when preparing the packet fails', async () => {
    packetResult = () => apiFailure(422, { code: 'packet_failed', message: 'no profile' })
    await mountDialog()
    expect($('job-packet-load-error')?.textContent).toContain('no profile')

    packetResult = () => application()
    await click('job-packet-retry')
    expect($('job-packet-load-error')).toBeNull()
    expect($('job-packet')).not.toBeNull()
  })
})

describe('editing and saving the packet', () => {
  it('renders every data-testid the flow requires', async () => {
    await mountDialog()
    expect($('job-cover-letter')).not.toBeNull()
    expect($('job-start-assist')).not.toBeNull()
    expect($('job-mark-submitted')).not.toBeNull()
  })

  it('renders screening drafts read-only', async () => {
    await mountDialog()
    expect($('job-screening-draft')?.textContent).toContain('Why us?')
    expect($('job-screening-draft')?.textContent).toContain('Because.')
  })

  it('calls the PUT fetcher with the edited cover letter text and shows saved', async () => {
    await mountDialog()
    await setInput('job-cover-letter', 'A rewritten cover letter.')
    await click('job-packet-save')

    const editCalls = calls.filter((call) => call.method === 'PUT' && call.path === '/api/jobs/applications/1/packet')
    expect(editCalls).toHaveLength(1)
    expect(editCalls[0]?.body).toEqual({ cover_letter_text: 'A rewritten cover letter.' })
    expect($('job-packet-saved')).not.toBeNull()
  })

  it('shows a save error and clears the saved indicator on further edits', async () => {
    await mountDialog()
    editResult = () => apiFailure(500, { message: 'boom' })
    await setInput('job-cover-letter', 'A rewritten cover letter.')
    await click('job-packet-save')
    expect($('job-packet-save-error')?.textContent).toContain('boom')
    expect($('job-packet-saved')).toBeNull()
  })
})

describe('starting assist and polling', () => {
  it('calls the queue fetcher, then shows the "not yet claimed" hint once queued past the threshold', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    await mountDialog()

    await click('job-start-assist')
    const queueCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/applications/1/assist')
    expect(queueCalls).toHaveLength(1)
    expect($('job-assist-state')?.textContent).toContain('Queued')
    expect($('job-assist-queued-hint')).toBeNull()

    listResult = () => [application({ status: 'ready', assist_state: 'queued' })]
    await vi.advanceTimersByTimeAsync(18_000)
    await flushPromises()

    expect($('job-assist-queued-hint')).not.toBeNull()
    expect($('job-assist-queued-hint')?.textContent).toContain('desktop helper')
    expect($('job-assist-command')?.textContent).toContain('python -m pipeline.jobs.assist --watch')
    vi.useRealTimers()
  })

  it('shows the live assist_state once claimed/running, replacing the hint', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    await mountDialog()
    await click('job-start-assist')

    listResult = () => [application({ status: 'ready', assist_state: 'queued' })]
    await vi.advanceTimersByTimeAsync(18_000)
    await flushPromises()
    expect($('job-assist-queued-hint')).not.toBeNull()

    listResult = () => [application({ status: 'ready', assist_state: 'running', assist_session_id: 'sess-1' })]
    await vi.advanceTimersByTimeAsync(3_000)
    await flushPromises()

    expect($('job-assist-queued-hint')).toBeNull()
    expect($('job-assist-state')?.textContent).toContain('Running')
    vi.useRealTimers()
  })
})

describe('marking submitted', () => {
  it('requires a confirm step before calling the PATCH fetcher', async () => {
    const wrapper = await mountDialog()

    await click('job-mark-submitted')
    let statusCalls = calls.filter((call) => call.method === 'PATCH' && call.path === '/api/jobs/applications/1')
    expect(statusCalls).toHaveLength(0)
    expect($('job-confirm-submitted')).not.toBeNull()

    await click('job-confirm-submitted')
    statusCalls = calls.filter((call) => call.method === 'PATCH' && call.path === '/api/jobs/applications/1')
    expect(statusCalls).toHaveLength(1)
    expect(statusCalls[0]?.body).toEqual({ status: 'submitted' })
    expect(wrapper.emitted('submitted')?.[0]?.[0]).toMatchObject({ status: 'submitted' })
  })

  it('surfaces an illegal-transition 422 via the error banner', async () => {
    await mountDialog()
    statusResult = () => apiFailure(422, { code: 'illegal_transition', message: 'cannot submit from saved' })

    await click('job-mark-submitted')
    await click('job-confirm-submitted')

    expect($('job-mark-submitted-error')?.textContent).toContain('cannot submit from saved')
  })

  it('disables mark-submitted when there is no packet', async () => {
    packetResult = () => application({ packet: null })
    await mountDialog()
    expect($<HTMLButtonElement>('job-mark-submitted')?.disabled).toBe(true)
  })
})

describe('closing', () => {
  it('emits update:open false and closed from the close button', async () => {
    const wrapper = await mountDialog()
    await click('job-apply-close')
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    expect(wrapper.emitted('closed')).toHaveLength(1)
  })
})
