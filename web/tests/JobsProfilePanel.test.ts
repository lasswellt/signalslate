import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import JobsProfilePanel from '~/components/JobsProfilePanel.vue'
import type { AnswerOut, JobsProfileOut } from '~/composables/useJobsApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function profile(overrides: Partial<JobsProfileOut> = {}): JobsProfileOut {
  return {
    id: 1,
    full_name: 'Ada Lovelace',
    email: 'ada@example.com',
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
    updated_at: new Date().toISOString(),
    ...overrides,
  }
}

function answer(overrides: Partial<AnswerOut> = {}): AnswerOut {
  return {
    id: 1,
    question_norm: 'why do you want to work here',
    question_raw: 'Why do you want to work here?',
    answer: 'Because the mission matters.',
    source_application_id: null,
    updated_at: new Date().toISOString(),
    ...overrides,
  }
}

// FileReader is a true browser external with no equivalent in this codebase's test setup: a
// straightforward fake that resolves readAsDataURL on the next microtask, matching the real API's
// async contract without depending on happy-dom's timer-based implementation.
class FakeFileReader {
  result: string | null = null
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  readAsDataURL(_blob: unknown) {
    void Promise.resolve().then(() => {
      this.result = 'data:application/pdf;base64,ZmFrZS1wZGY='
      this.onload?.()
    })
  }
}

let profileResult: () => unknown
let updateProfileResult: () => unknown
let uploadResumeResult: () => unknown
let listAnswersResult: () => unknown
let createAnswerResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'GET' && path === '/api/jobs/profile') {
        const result = profileResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'PUT' && path === '/api/jobs/profile') {
        const result = updateProfileResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/profile/resume') {
        const result = uploadResumeResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'GET' && path === '/api/jobs/answers') {
        const result = listAnswersResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/answers') {
        const result = createAnswerResult()
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

async function mountPanel() {
  const wrapper = await mountSuspended(JobsProfilePanel, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  profileResult = () => profile()
  updateProfileResult = () => profile({ full_name: 'Ada Updated' })
  uploadResumeResult = () => ({ resume_paths: ['/data/resumes/resume.pdf'] })
  listAnswersResult = () => [
    answer({ id: 1, question_raw: 'Why do you want to work here?', answer: 'Because the mission matters.' }),
    answer({ id: 2, question_raw: 'What is your greatest strength?', answer: 'Persistence.', question_norm: 'what is your greatest strength' }),
  ]
  createAnswerResult = () => answer({ id: 3, answer: 'An edited answer.' })
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('rendering', () => {
  it('renders the resume file picker and the answer bank table', async () => {
    await mountPanel()
    expect($('jobs-resume-file')).not.toBeNull()
    expect($('jobs-answer-bank')).not.toBeNull()
  })
})

describe('resume upload', () => {
  it('uploads the chosen file and shows the file name (never a server path)', async () => {
    vi.stubGlobal('FileReader', FakeFileReader)
    await mountPanel()
    const file = new File(['fake-pdf-content'], 'resume.pdf', { type: 'application/pdf' })
    const input = $<HTMLInputElement>('jobs-resume-file')!
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    await click('jobs-resume-upload')

    const uploadCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/profile/resume')
    expect(uploadCalls).toHaveLength(1)
    expect(uploadCalls[0]?.body).toEqual({ filename: 'resume.pdf', content_b64: expect.any(String) })
    expect($('jobs-resume-files')?.textContent).toContain('resume.pdf')
    expect($('jobs-resume-files')?.textContent).not.toContain('/data/resumes')
  })

  it('shows an error banner when the upload is rejected', async () => {
    vi.stubGlobal('FileReader', FakeFileReader)
    uploadResumeResult = () => apiFailure(422, { message: 'file too large' })
    await mountPanel()
    const file = new File(['fake-pdf-content'], 'resume.pdf', { type: 'application/pdf' })
    const input = $<HTMLInputElement>('jobs-resume-file')!
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    await click('jobs-resume-upload')

    expect($('jobs-resume-error')?.textContent ?? '').toContain('file too large')
  })
})

describe('answer bank', () => {
  it('filters rendered rows by the search text', async () => {
    await mountPanel()
    expect($('jobs-answer-row-1')).not.toBeNull()
    expect($('jobs-answer-row-2')).not.toBeNull()

    await setInput('jobs-answer-search', 'greatest strength')

    expect($('jobs-answer-row-1')).toBeNull()
    expect($('jobs-answer-row-2')).not.toBeNull()
  })
})

describe('dirty tracking and validation', () => {
  it('enables Save only once dirty and valid, and Discard reverts the form', async () => {
    await mountPanel()
    expect(($('jobs-profile-save') as HTMLButtonElement).disabled).toBe(true)

    await setInput('jobs-email', 'not-an-email')
    expect(($('jobs-profile-save') as HTMLButtonElement).disabled).toBe(true)

    await setInput('jobs-email', 'grace@example.com')
    expect(($('jobs-profile-save') as HTMLButtonElement).disabled).toBe(false)
    expect(($('jobs-profile-discard') as HTMLButtonElement).disabled).toBe(false)

    await click('jobs-profile-discard')
    expect(($('jobs-email') as HTMLInputElement).value).toBe('ada@example.com')
    expect(($('jobs-profile-save') as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('saving the profile', () => {
  it('calls the profile PUT fetcher with the form current values', async () => {
    await mountPanel()
    await setInput('jobs-full-name', 'Grace Hopper')
    await setInput('jobs-email', 'grace@example.com')

    await click('jobs-profile-save')

    const saveCalls = calls.filter((call) => call.method === 'PUT' && call.path === '/api/jobs/profile')
    expect(saveCalls).toHaveLength(1)
    expect(saveCalls[0]?.body).toMatchObject({ full_name: 'Grace Hopper', email: 'grace@example.com' })
    expect(body.textContent).toContain('Profile saved.')
  })
})
