import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import JobDetailDialog from '~/components/JobDetailDialog.vue'
import type { ApplicationOut, PostingOut } from '~/composables/useJobsApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function posting(overrides: Partial<PostingOut> = {}): PostingOut {
  return {
    id: 42,
    title: 'Staff Engineer',
    location: 'Remote (US)',
    remote: true,
    comp_text: '$180k-$220k',
    apply_url: 'https://boards.example.com/jobs/42',
    first_seen: '2026-09-01T00:00:00Z',
    last_seen: '2026-09-20T00:00:00Z',
    closed_at: null,
    fit_score: 82,
    fit_reason: 'Strong match on backend and infra experience.',
    company_name: 'Acme Corp',
    ats_kind: 'greenhouse',
    ...overrides,
  }
}

function application(overrides: Partial<ApplicationOut> = {}): ApplicationOut {
  return {
    id: 7,
    posting_id: 42,
    status: 'saved',
    packet: null,
    cover_letter_path: null,
    resume_path: null,
    assist_state: 'idle',
    assist_session_id: null,
    created_at: '2026-09-22T00:00:00Z',
    submitted_at: null,
    ...overrides,
  }
}

let createResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'POST' && path === '/api/jobs/applications') {
        const result = createResult()
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

let mounted: Array<VueWrapper<unknown>> = []

async function mountDialog(overrides: Partial<PostingOut> = {}) {
  const wrapper = await mountSuspended(JobDetailDialog, { props: { open: true, posting: posting(overrides) } })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  createResult = () => application()
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('posting detail', () => {
  it('renders title, fit reason and details', async () => {
    await mountDialog()
    expect($('dialog-title')?.textContent).toContain('Staff Engineer')
    expect($('dialog-subtitle')?.textContent).toContain('Acme Corp')
    expect($('job-fit-reason')?.textContent).toContain('Strong match on backend and infra experience.')
    expect($('job-fit-score')?.textContent).toContain('82')
    expect($('job-details')?.textContent).toContain('$180k-$220k')
    expect($('job-closed-banner')).toBeNull()
  })

  it('shows a closed banner when closed_at is set', async () => {
    await mountDialog({ closed_at: '2026-09-15T00:00:00Z' })
    expect($('job-closed-banner')).not.toBeNull()
  })
})

describe('start application', () => {
  it('calls createApplication with the posting id and shows a success state', async () => {
    await mountDialog()
    expect($('job-start-application')).not.toBeNull()

    await click('job-start-application')

    expect(
      calls.some(
        (call) => call.method === 'POST' && call.path === '/api/jobs/applications' && call.body?.posting_id === 42,
      ),
    ).toBe(true)
    expect($('job-application-created')).not.toBeNull()
    expect($('job-start-application')).toBeNull()
  })

  it('emits applicationCreated with the created application', async () => {
    const wrapper = await mountDialog()
    await click('job-start-application')
    expect(wrapper.emitted('applicationCreated')?.at(0)).toEqual([application()])
  })

  it('shows an error banner when the create call fails', async () => {
    createResult = () => apiFailure(500, { message: 'creation failed' })
    await mountDialog()
    await click('job-start-application')
    expect($('job-start-application-error')?.textContent).toContain('creation failed')
    expect($('job-start-application')).not.toBeNull()
  })
})

describe('closing', () => {
  it('emits update:open false and closed from the close button', async () => {
    const wrapper = await mountDialog()
    await click('job-detail-close')
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    expect(wrapper.emitted('closed')).toHaveLength(1)
  })
})
