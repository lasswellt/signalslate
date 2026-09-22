import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import JobsPage from '~/pages/jobs.vue'
import type { ApplicationOut, CompanyOut, JobsProfileOut, PostingOut } from '~/composables/useJobsApi'

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

function company(overrides: Partial<CompanyOut> = {}): CompanyOut {
  return {
    id: 1,
    name: 'Acme Corp',
    domain: 'acme.com',
    source: 'manual',
    status: 'active',
    first_seen: '2026-09-01T00:00:00Z',
    last_seen: '2026-09-20T00:00:00Z',
    boards: [],
    ...overrides,
  }
}

function profile(overrides: Partial<JobsProfileOut> = {}): JobsProfileOut {
  return {
    id: 1,
    full_name: null,
    email: null,
    phone: null,
    linkedin_url: null,
    github_url: null,
    portfolio_url: null,
    other_links: [],
    work_authorized: null,
    needs_sponsorship: null,
    open_to_relocation: null,
    relocation_notes: null,
    start_date_notes: null,
    salary_floor: null,
    salary_disclosure_policy: 'decline',
    eeo_answers: {},
    target_roles: [],
    target_locations: [],
    target_remote: null,
    target_salary_floor: null,
    target_exclusions: [],
    resume_paths: [],
    cover_letter_tone: null,
    updated_at: null,
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
  companies?: CompanyOut[] | Error
  profile?: JobsProfileOut | Error
  answers?: unknown[] | Error
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
      if (method === 'GET' && path === '/api/jobs/companies') return answer(routes.companies ?? [])
      if (method === 'GET' && path === '/api/jobs/profile') return answer(routes.profile ?? profile())
      if (method === 'GET' && path === '/api/jobs/answers') return answer(routes.answers ?? [])
      throw new Error(`unexpected ${method} ${path}`)
    },
  )
}

const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(JobsPage))),
})

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountPage() {
  const wrapper = await mountSuspended(Harness, { attachTo: document.body })
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
})

describe('jobs page', () => {
  it('shows the four tabs and switches between panels', async () => {
    stubApi({
      postings: [posting()],
      companies: [company({ name: 'Beta Inc' })],
      profile: profile({ full_name: 'Ada Lovelace' }),
    })
    const wrapper = await mountPage()

    expect(wrapper.find('[data-testid="panel-postings"]').isVisible()).toBe(true)
    expect(wrapper.find('[data-testid="postings-table"]').exists()).toBe(true)

    await click('tab-applications')
    expect(wrapper.find('[data-testid="panel-applications"]').isVisible()).toBe(true)

    await click('tab-companies')
    expect($('jobs-companies-panel')).not.toBeNull()
    expect($('jobs-companies-panel')?.textContent).toContain('Beta Inc')

    await click('tab-profile')
    expect($('jobs-profile-panel')).not.toBeNull()
    expect($<HTMLInputElement>('jobs-full-name')?.value).toBe('Ada Lovelace')
  })

  it('renders fetched postings sorted by fit score then first_seen, both descending', async () => {
    stubApi({
      postings: [
        posting({ id: 1, title: 'Low fit, newer', fit_score: 40, first_seen: '2026-09-10T00:00:00Z' }),
        posting({ id: 2, title: 'High fit', fit_score: 90, first_seen: '2026-09-01T00:00:00Z' }),
        posting({ id: 3, title: 'Low fit, older', fit_score: 40, first_seen: '2026-09-05T00:00:00Z' }),
      ],
    })
    const wrapper = await mountPage()

    const rows = wrapper.findAll('[data-testid="postings-table"] tbody tr')
    expect(rows).toHaveLength(3)
    expect(rows[0].text()).toContain('High fit')
    expect(rows[1].text()).toContain('Low fit, newer')
    expect(rows[2].text()).toContain('Low fit, older')
  })

  it('re-fetches postings with the min-score filter applied', async () => {
    stubApi({ postings: [posting()] })
    const wrapper = await mountPage()

    const input = $<HTMLInputElement>('postings-filter-min-score')
    if (!input) throw new Error('no min score input')
    input.value = '75'
    input.dispatchEvent(new Event('input'))
    await flushPromises()

    const call = calls.find((c) => c.method === 'GET' && c.path === '/api/jobs/postings' && c.params?.min_score === '75')
    expect(call).toBeDefined()
    expect(wrapper).toBeTruthy()
  })

  it('opens JobDetailDialog when a posting row is clicked', async () => {
    stubApi({ postings: [posting({ id: 5, title: 'Clickable Role' })] })
    await mountPage()

    await click('posting-row-5')

    expect($('job-detail-dialog')).not.toBeNull()
    expect($('job-title')?.textContent).toContain('Clickable Role')
  })

  it('flows from starting an application into JobApplyDialog', async () => {
    stubApi({
      postings: [posting({ id: 9 })],
      createApplication: () => application({ id: 42, posting_id: 9 }),
    })
    await mountPage()

    await click('posting-row-9')
    await click('job-start-application')

    expect($('job-apply-dialog')).not.toBeNull()
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/jobs/applications/42/packet')).toBe(true)
  })

  it('lists applications and opens JobApplyDialog on row click', async () => {
    stubApi({ applications: [application({ id: 11 })] })
    const wrapper = await mountPage()

    await click('tab-applications')
    expect(wrapper.get('[data-testid="applications-table"]').text()).toContain('11')

    await click('application-row-11')
    expect($('job-apply-dialog')).not.toBeNull()
  })
})
