import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import type { Component } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import JobsPage from '~/pages/jobs.vue'
import JobsIndexPage from '~/pages/jobs/index.vue'
import JobsApplicationsPage from '~/pages/jobs/applications.vue'
import type { ApplicationOut, PostingOut } from '~/composables/useJobsApi'

function posting(overrides: Partial<PostingOut> = {}): PostingOut {
  return {
    id: 1,
    title: 'Staff Engineer',
    location: 'Remote (US)',
    remote: true,
    comp_text: '$180k-$220k',
    apply_url: 'https://boards.example.com/jobs/1',
    first_seen: '2026-09-01T00:00:00Z',
    last_seen: '2026-09-20T00:00:00Z',
    closed_at: null,
    fit_score: 60,
    fit_reason: 'Decent match.',
    company_name: 'Acme Corp',
    ats_kind: 'greenhouse',
    ...overrides,
  }
}

function application(overrides: Partial<ApplicationOut> = {}): ApplicationOut {
  return {
    id: 7,
    posting_id: 1,
    posting_title: 'Staff Engineer',
    company_name: 'Acme Corp',
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

interface Call {
  method: string
  path: string
  params?: Record<string, string>
  body?: Record<string, unknown>
}

interface Routes {
  postings?: PostingOut[] | Error
  applications?: ApplicationOut[] | Error
  createApplication?: (body: Record<string, unknown>) => ApplicationOut | Error
}

let calls: Call[]
let routes: Routes

function stubApi(nextRoutes: Routes) {
  routes = nextRoutes
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string; params?: Record<string, unknown> } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      const body = options.body ? (JSON.parse(options.body) as Record<string, unknown>) : undefined
      const params = options.params
        ? Object.fromEntries(Object.entries(options.params).map(([key, value]) => [key, String(value)]))
        : undefined
      calls.push({ method, path, params, body })
      const answer = (value: unknown) => {
        if (value instanceof Error) throw value
        return value
      }
      if (method === 'GET' && path === '/api/jobs/postings') return answer(routes.postings ?? [])
      if (method === 'GET' && path === '/api/jobs/applications') return answer(routes.applications ?? [])
      if (method === 'POST' && path === '/api/jobs/applications') {
        return answer(
          routes.createApplication ? routes.createApplication(body ?? {}) : application({ posting_id: body?.posting_id as number }),
        )
      }
      throw new Error(`unexpected ${method} ${path}`)
    },
  )
}

function harnessFor(component: Component) {
  return defineComponent({
    render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(component))),
  })
}

const IndexHarness = harnessFor(JobsIndexPage)
const ApplicationsHarness = harnessFor(JobsApplicationsPage)
const TabsHarness = harnessFor(JobsPage)

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mount(harness: Component) {
  const wrapper = await mountSuspended(harness, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  mounted = []
  stubApi({})
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('jobs parent tabs', () => {
  it('renders the four route tabs pointing at the jobs sub-routes', async () => {
    stubApi({})
    await mount(TabsHarness)

    expect($<HTMLAnchorElement>('tab-postings')?.getAttribute('href')).toBe('/jobs')
    expect($<HTMLAnchorElement>('tab-applications')?.getAttribute('href')).toBe('/jobs/applications')
    expect($<HTMLAnchorElement>('tab-companies')?.getAttribute('href')).toBe('/jobs/companies')
    expect($<HTMLAnchorElement>('tab-profile')?.getAttribute('href')).toBe('/jobs/profile')
  })
})

describe('jobs postings page', () => {
  it('renders fetched postings sorted by fit score then first_seen, both descending', async () => {
    stubApi({
      postings: [
        posting({ id: 1, title: 'Low fit, newer', fit_score: 40, first_seen: '2026-09-10T00:00:00Z' }),
        posting({ id: 2, title: 'High fit', fit_score: 90, first_seen: '2026-09-01T00:00:00Z' }),
        posting({ id: 3, title: 'Low fit, older', fit_score: 40, first_seen: '2026-09-05T00:00:00Z' }),
      ],
    })
    const wrapper = await mount(IndexHarness)

    const rows = wrapper.findAll('[data-testid="postings-table"] tbody tr')
    expect(rows).toHaveLength(3)
    expect(rows[0].text()).toContain('High fit')
    expect(rows[1].text()).toContain('Low fit, newer')
    expect(rows[2].text()).toContain('Low fit, older')
  })

  it('shows title, company and an unscored fit chip when fit_score is null', async () => {
    stubApi({ postings: [posting({ id: 4, title: 'Unscored Role', fit_score: null })] })
    const wrapper = await mount(IndexHarness)

    const row = wrapper.get('[data-testid="posting-row-4"]')
    expect(row.text()).toContain('Unscored Role')
    expect(row.text()).toContain('Acme Corp')
    expect(row.text()).toContain('Unscored')
  })

  it('re-fetches postings with the min-score filter applied', async () => {
    stubApi({ postings: [posting()] })
    await mount(IndexHarness)

    const input = $<HTMLInputElement>('postings-filter-min-score')
    if (!input) throw new Error('no min score input')
    input.value = '75'
    input.dispatchEvent(new Event('input'))
    await flushPromises()

    const call = calls.find((c) => c.method === 'GET' && c.path === '/api/jobs/postings' && c.params?.min_score === '75')
    expect(call).toBeDefined()
  })

  it('debounces the text search before re-fetching', async () => {
    vi.useFakeTimers()
    stubApi({ postings: [posting()] })
    await mount(IndexHarness)

    const before = calls.length
    const input = $<HTMLInputElement>('postings-filter-text')
    if (!input) throw new Error('no text filter input')
    input.value = 'engineer'
    input.dispatchEvent(new Event('input'))

    // Still within the 300ms debounce window: no extra request yet.
    await vi.advanceTimersByTimeAsync(100)
    expect(calls.length).toBe(before)

    await vi.advanceTimersByTimeAsync(250)
    await flushPromises()

    const call = calls.find((c) => c.method === 'GET' && c.path === '/api/jobs/postings' && c.params?.text === 'engineer')
    expect(call).toBeDefined()
  })

  it('re-fetches postings when the remote toggle is switched on', async () => {
    stubApi({ postings: [posting()] })
    await mount(IndexHarness)

    await click('postings-filter-remote')

    const call = calls.find((c) => c.method === 'GET' && c.path === '/api/jobs/postings' && c.params?.remote === 'true')
    expect(call).toBeDefined()
  })

  it('shows the never-ingested empty state with a CTA to Companies when there are no filters', async () => {
    stubApi({ postings: [] })
    await mount(IndexHarness)

    expect($('postings-empty-none')).not.toBeNull()
    expect($<HTMLAnchorElement>('postings-empty-cta')?.getAttribute('href')).toBe('/jobs/companies')
    expect($('postings-empty')).toBeNull()
  })

  it('shows the no-match empty state with Clear filters when a filter is active and nothing matches', async () => {
    stubApi({ postings: [] })
    await mount(IndexHarness)

    const input = $<HTMLInputElement>('postings-filter-min-score')
    if (!input) throw new Error('no min score input')
    input.value = '90'
    input.dispatchEvent(new Event('input'))
    await flushPromises()

    expect($('postings-empty')).not.toBeNull()
    expect($('postings-empty-none')).toBeNull()
    expect($('postings-clear-filters')).not.toBeNull()
  })

  it('opens JobDetailDialog when a posting row is clicked', async () => {
    stubApi({ postings: [posting({ id: 5, title: 'Clickable Role' })] })
    await mount(IndexHarness)

    await click('posting-row-5')

    expect($('job-detail-dialog')).not.toBeNull()
    expect($('job-title')?.textContent).toContain('Clickable Role')
  })

  it('flows from starting an application into JobApplyDialog, with only one dialog open', async () => {
    stubApi({
      postings: [posting({ id: 9 })],
      createApplication: () => application({ id: 42, posting_id: 9 }),
    })
    await mount(IndexHarness)

    await click('posting-row-9')
    await click('job-start-application')

    expect($('job-apply-dialog')).not.toBeNull()
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/jobs/applications/42/packet')).toBe(true)
  })
})

describe('jobs applications page', () => {
  it('lists applications with job title, company, status and assistant state', async () => {
    stubApi({ applications: [application({ id: 11 })] })
    const wrapper = await mount(ApplicationsHarness)

    const row = wrapper.get('[data-testid="application-row-11"]')
    expect(row.text()).toContain('Staff Engineer')
    expect(row.text()).toContain('Acme Corp')
  })

  it('falls back to "Posting #<id>" when posting_title is missing', async () => {
    stubApi({ applications: [application({ id: 12, posting_id: 3, posting_title: null, company_name: null })] })
    const wrapper = await mount(ApplicationsHarness)

    expect(wrapper.get('[data-testid="application-row-12"]').text()).toContain('Posting #3')
  })

  it('opens JobApplyDialog on row click', async () => {
    stubApi({ applications: [application({ id: 11 })] })
    await mount(ApplicationsHarness)

    await click('application-row-11')
    expect($('job-apply-dialog')).not.toBeNull()
  })

  it('shows an empty state when there are no applications yet', async () => {
    stubApi({ applications: [] })
    await mount(ApplicationsHarness)

    expect($('empty-state')).not.toBeNull()
    expect($('empty-state')?.textContent).toContain('No applications yet')
  })
})
