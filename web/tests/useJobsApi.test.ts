import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '~/composables/useApi'
import { useJobsApi } from '~/composables/useJobsApi'

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({})
  vi.stubGlobal('$fetch', fetchMock)
})

async function failure(call: () => Promise<unknown>): Promise<ApiError> {
  try {
    await call()
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError)
    return error as ApiError
  }
  throw new Error('expected the call to fail')
}

describe('useJobsApi', () => {
  const api = () => useJobsApi()
  const cases: Array<[string, string, string, (client: ReturnType<typeof useJobsApi>) => Promise<unknown>]> = [
    ['addCompany', 'POST', '/api/jobs/companies', (c) => c.addCompany({ name: 'Acme', domain: 'acme.com' })],
    ['importCompanies', 'POST', '/api/jobs/companies/import', (c) => c.importCompanies({ csv: 'acme.com\n' })],
    ['importCompaniesYc', 'POST', '/api/jobs/companies/import-yc', (c) => c.importCompaniesYc()],
    ['importCompaniesHn', 'POST', '/api/jobs/companies/import-hn', (c) => c.importCompaniesHn()],
    ['rescanCompany', 'POST', '/api/jobs/companies/1/rescan', (c) => c.rescanCompany(1)],
    [
      'overrideBoard',
      'PUT',
      '/api/jobs/boards/2',
      (c) => c.overrideBoard(2, { ats_kind: 'greenhouse', board_id: 'acme' }),
    ],
    [
      'updateProfile',
      'PUT',
      '/api/jobs/profile',
      (c) => c.updateProfile({ full_name: 'Jane Doe', salary_disclosure_policy: 'decline' }),
    ],
    [
      'uploadResume',
      'POST',
      '/api/jobs/profile/resume',
      (c) => c.uploadResume({ filename: 'resume.pdf', content_b64: 'JVBERi0=' }),
    ],
    [
      'createAnswer',
      'POST',
      '/api/jobs/answers',
      (c) => c.createAnswer({ question_raw: 'Why us?', answer: 'Because.' }),
    ],
    ['createApplication', 'POST', '/api/jobs/applications', (c) => c.createApplication({ posting_id: 5 })],
    [
      'updateApplicationStatus',
      'PATCH',
      '/api/jobs/applications/5',
      (c) => c.updateApplicationStatus(5, { status: 'ready' }),
    ],
    ['prepareApplicationPacket', 'POST', '/api/jobs/applications/5/packet', (c) => c.prepareApplicationPacket(5)],
    [
      'editApplicationPacket',
      'PUT',
      '/api/jobs/applications/5/packet',
      (c) => c.editApplicationPacket(5, { cover_letter_text: 'Dear hiring manager,' }),
    ],
    ['queueAssist', 'POST', '/api/jobs/applications/5/assist', (c) => c.queueAssist(5)],
    ['claimAssist', 'POST', '/api/jobs/assist/queue/5/claim', (c) => c.claimAssist(5)],
    [
      'updateAssistSession',
      'PATCH',
      '/api/jobs/assist/sessions/sess-1',
      (c) => c.updateAssistSession('sess-1', { assist_state: 'running' }),
    ],
    [
      'proposeSessionValues',
      'POST',
      '/api/jobs/assist/sessions/sess-1/propose',
      (c) =>
        c.proposeSessionValues('sess-1', {
          fields: [{ field_id: 'email', label: 'Email' }],
        }),
    ],
  ]

  it.each(cases)('%s sends the CSRF header and a JSON content type', async (_name, method, path, call) => {
    await call(api())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, options] = fetchMock.mock.calls[0] as [string, { method: string; headers: Record<string, string> }]
    expect(url.endsWith(path)).toBe(true)
    expect(options.method).toBe(method)
    expect(options.headers['X-Requested-With']).toBe('signalslate')
    expect(options.headers['Content-Type']).toBe('application/json')
  })

  it('sends the body as a JSON string', async () => {
    await api().createApplication({ posting_id: 42 })
    const [, options] = fetchMock.mock.calls[0] as [string, { body?: string }]
    expect(JSON.parse(options.body ?? '')).toEqual({ posting_id: 42 })
  })

  it('sends no body for a body-less POST', async () => {
    await api().rescanCompany(1)
    const [, options] = fetchMock.mock.calls[0] as [string, { body?: string }]
    expect(options.body).toBeUndefined()
  })

  it('does not send the header on reads, and keeps the query params', async () => {
    const client = api()
    await client.listCompanies({ status: 'active' })
    await client.listPostings({ minScore: 7, status: 'open', company: 'acme', text: 'staff', remote: true })
    await client.listAnswers('why us')
    await client.listApplications('ready')
    await client.getAssistQueueHead()
    for (const [, options] of fetchMock.mock.calls as Array<[string, { headers?: unknown; method: string }]>) {
      expect(options.method).toBe('GET')
      expect(options.headers).toBeUndefined()
    }
    const [companiesUrl, companiesOptions] = fetchMock.mock.calls[0] as [string, { params: unknown }]
    expect(companiesUrl.endsWith('/api/jobs/companies')).toBe(true)
    expect(companiesOptions.params).toEqual({ status: 'active' })

    const [postingsUrl, postingsOptions] = fetchMock.mock.calls[1] as [string, { params: unknown }]
    expect(postingsUrl.endsWith('/api/jobs/postings')).toBe(true)
    expect(postingsOptions.params).toEqual({
      min_score: 7,
      status: 'open',
      company: 'acme',
      text: 'staff',
      remote: true,
    })

    const [answersUrl, answersOptions] = fetchMock.mock.calls[2] as [string, { params: unknown }]
    expect(answersUrl.endsWith('/api/jobs/answers')).toBe(true)
    expect(answersOptions.params).toEqual({ q: 'why us' })

    const [applicationsUrl, applicationsOptions] = fetchMock.mock.calls[3] as [string, { params: unknown }]
    expect(applicationsUrl.endsWith('/api/jobs/applications')).toBe(true)
    expect(applicationsOptions.params).toEqual({ status: 'ready' })

    expect((fetchMock.mock.calls[4] as [string])[0].endsWith('/api/jobs/assist/queue')).toBe(true)
  })

  it('drops undefined query params', async () => {
    await api().listCompanies()
    const [, options] = fetchMock.mock.calls[0] as [string, { params: unknown }]
    expect(options.params).toEqual({})
  })

  it('escapes path segments', async () => {
    await api().updateAssistSession('a/b c', { step: 'x' })
    expect((fetchMock.mock.calls[0] as [string])[0].endsWith('/api/jobs/assist/sessions/a%2Fb%20c')).toBe(true)
  })

  it('builds the application file URL without calling $fetch', () => {
    const fileUrl = api().applicationFileUrl(7, 'resume')
    expect(fileUrl.endsWith('/api/jobs/applications/7/files/resume')).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  describe('error normalization', () => {
    function fetchError(status: number, data: unknown): Error {
      return Object.assign(new Error(`[POST] "http://api.example.com/x": ${status}`), { status, statusCode: status, data })
    }

    it('reads a coded detail as code and message', async () => {
      fetchMock.mockRejectedValue(
        fetchError(422, { detail: { code: 'not_ready', message: "Application status must be 'ready' to queue assist" } }),
      )
      const error = await failure(() => api().queueAssist(5))
      expect(error).toMatchObject({
        status: 422,
        code: 'not_ready',
        message: "Application status must be 'ready' to queue assist",
      })
    })

    it('reads a plain string detail (application_not_found)', async () => {
      fetchMock.mockRejectedValue(fetchError(404, { detail: 'Application not found' }))
      const error = await failure(() => api().prepareApplicationPacket(999))
      expect(error).toMatchObject({ status: 404, message: 'Application not found' })
    })
  })
})
