import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, parseUtc, useApi } from '~/composables/useApi'

const SECRET = 'sk-live-0123456789-not-a-real-secret'

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({})
  vi.stubGlobal('$fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/** What ofetch throws on a non-2xx: a message with the URL, the status, the parsed body and the request options. */
function fetchError(status: number, data: unknown, requestBody?: string): Error {
  return Object.assign(new Error(`[POST] "http://api.example.com/x": ${status} ${requestBody ?? ''}`), {
    status,
    statusCode: status,
    data,
    options: { body: requestBody },
  })
}

async function failure(call: () => Promise<unknown>): Promise<ApiError> {
  try {
    await call()
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError)
    return error as ApiError
  }
  throw new Error('expected the call to fail')
}

describe('mutating calls', () => {
  const api = () => useApi()
  const cases: Array<[string, string, string, (client: ReturnType<typeof useApi>) => Promise<unknown>]> = [
    ['triggerRun', 'POST', '/api/runs/trigger', (c) => c.triggerRun()],
    [
      'updateConfig',
      'PUT',
      '/api/config',
      (c) => c.updateConfig({ schedule_cron: '0 7 * * *', tracker: 'jira', active_sources: {} }),
    ],
    [
      'createConnection',
      'POST',
      '/api/connections',
      (c) => c.createConnection({ kind: 'slack', label: 'acme', token: SECRET }),
    ],
    [
      'updateConnection',
      'PATCH',
      '/api/connections/slack_acme',
      (c) => c.updateConnection('slack_acme', { secrets: { token: SECRET } }),
    ],
    ['deleteConnection', 'DELETE', '/api/connections/slack_acme', (c) => c.deleteConnection('slack_acme')],
    ['testConnection', 'POST', '/api/connections/slack_acme/test', (c) => c.testConnection('slack_acme')],
    ['runCollector', 'POST', '/api/collectors/zoom/run', (c) => c.runCollector('zoom')],
    ['startDryRun', 'POST', '/api/collectors/zoom/dry-run', (c) => c.startDryRun('zoom', { hours: 6, limit: 3 })],
    ['resetCollector', 'POST', '/api/collectors/zoom/reset', (c) => c.resetCollector('zoom', 2)],
    ['clearFailures', 'POST', '/api/collectors/zoom/clear-failures', (c) => c.clearFailures('zoom')],
    [
      'oauthStart',
      'POST',
      '/api/oauth/google/start',
      (c) => c.oauthStart('google', { connection_id: 'gmail_work', mode: 'paste_back' }),
    ],
    [
      'oauthPaste',
      'POST',
      '/api/oauth/microsoft/paste',
      (c) => c.oauthPaste('microsoft', { flow_id: 'f1', url: 'https://example.com/cb?code=abc&state=xyz' }),
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

  it('sends the body as a JSON string, and no body for a body-less POST', async () => {
    const client = api()
    await client.createConnection({ kind: 'slack', label: 'acme', token: SECRET })
    await client.testConnection('slack_acme')
    const [, withBody] = fetchMock.mock.calls[0] as [string, { body?: string }]
    const [, withoutBody] = fetchMock.mock.calls[1] as [string, { body?: string }]
    expect(JSON.parse(withBody.body ?? '')).toEqual({ kind: 'slack', label: 'acme', token: SECRET })
    expect(withoutBody.body).toBeUndefined()
  })

  it('sends days_back null when a reset has no window', async () => {
    await api().resetCollector('zoom')
    const [, options] = fetchMock.mock.calls[0] as [string, { body: string }]
    expect(JSON.parse(options.body)).toEqual({ days_back: null })
  })

  it('does not send the header on reads, and keeps the query params', async () => {
    const client = api()
    await client.getStatus()
    await client.getRuns(10)
    await client.listItems('zoom', { limit: 25, beforeId: 90 })
    for (const [, options] of fetchMock.mock.calls as Array<[string, { headers?: unknown; method: string }]>) {
      expect(options.method).toBe('GET')
      expect(options.headers).toBeUndefined()
    }
    expect((fetchMock.mock.calls[1] as [string, { params: unknown }])[1].params).toEqual({ limit: 10 })
    const [itemsUrl, itemsOptions] = fetchMock.mock.calls[2] as [string, { params: unknown }]
    expect(itemsUrl.endsWith('/api/collectors/zoom/items')).toBe(true)
    expect(itemsOptions.params).toEqual({ limit: 25, before_id: 90 })
  })

  it('sends credentials include on oauthStart and oauthPaste only', async () => {
    const client = api()
    const withCredentials = new Set(['oauthStart', 'oauthPaste'])
    for (const [name, , , call] of cases) {
      fetchMock.mockClear()
      await call(client)
      const [, options] = fetchMock.mock.calls[0] as [string, { credentials?: string }]
      if (withCredentials.has(name)) expect(options.credentials, name).toBe('include')
      else expect(options, name).not.toHaveProperty('credentials')
    }
    fetchMock.mockClear()
    await client.getStatus()
    await client.listItems('zoom')
    for (const [, options] of fetchMock.mock.calls as Array<[string, object]>) {
      expect(options).not.toHaveProperty('credentials')
    }
  })

  it('escapes path segments', async () => {
    await api().deleteConnection('a/b c')
    expect((fetchMock.mock.calls[0] as [string])[0].endsWith('/api/connections/a%2Fb%20c')).toBe(true)
  })

  it('keeps pdfUrl', () => {
    expect(api().pdfUrl(7).endsWith('/api/runs/7/pdf')).toBe(true)
  })
})

describe('error normalization', () => {
  it('turns a 422 detail array into a readable message without the submitted values', async () => {
    fetchMock.mockRejectedValue(
      fetchError(422, {
        detail: [
          { type: 'string_too_short', loc: ['body', 'token'], msg: 'String should have at least 10 characters' },
          { type: 'value_error', loc: ['body', 'config', 'tenant_id'], msg: 'must be a UUID' },
          // A backend that still echoed `input` must not leak it through the client either.
          { type: 'value_error', loc: ['body', 'client_secret'], msg: 'too short', input: SECRET },
        ],
      }),
    )
    const error = await failure(() => useApi().createConnection({ kind: 'slack', label: 'acme', token: SECRET }))
    expect(error.status).toBe(422)
    expect(error.code).toBeUndefined()
    expect(error.message).toBe(
      'token: String should have at least 10 characters; config.tenant_id: must be a UUID; client_secret: too short',
    )
    expect(error.message).not.toContain(SECRET)
  })

  it('reads a coded detail as code and message', async () => {
    fetchMock.mockRejectedValue(
      fetchError(503, { detail: { code: 'secret_key_missing', message: 'No encryption key is configured' } }),
    )
    const error = await failure(() => useApi().testConnection('zoom'))
    expect(error).toMatchObject({ status: 503, code: 'secret_key_missing', message: 'No encryption key is configured' })
  })

  it('reads a plain string detail (the CSRF guard and 404s)', async () => {
    fetchMock.mockRejectedValue(fetchError(403, { detail: 'X-Requested-With header required' }))
    const error = await failure(() => useApi().triggerRun())
    expect(error).toMatchObject({ status: 403, message: 'X-Requested-With header required' })
  })

  it('falls back to a generic message when there is no detail or no response at all', async () => {
    fetchMock.mockRejectedValueOnce(fetchError(500, undefined))
    expect((await failure(() => useApi().getStatus())).message).toBe('Request failed (500)')
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const offline = await failure(() => useApi().getStatus())
    expect(offline.status).toBe(0)
    expect(offline.message).toBe('The API could not be reached')
  })

  it('normalizes read failures too', async () => {
    fetchMock.mockRejectedValue(fetchError(404, { detail: { code: 'unknown_source', message: 'Unknown source' } }))
    const error = await failure(() => useApi().listItems('nope'))
    expect(error).toMatchObject({ status: 404, code: 'unknown_source' })
  })

  it('never lets the request body reach the ApiError', async () => {
    const body = JSON.stringify({ kind: 'slack', label: 'acme', token: SECRET })
    // The fetch error itself quotes the body in its message and options, and the response detail
    // is a shape the client does not recognise: nothing of that may be copied over.
    fetchMock.mockRejectedValue(fetchError(400, { detail: { unexpected: SECRET }, echoed: body }, body))
    const error = await failure(() => useApi().createConnection({ kind: 'slack', label: 'acme', token: SECRET }))
    expect(error.message).toBe('Request failed (400)')
    expect(error.cause).toBeUndefined()
    const everything = JSON.stringify(error, Object.getOwnPropertyNames(error))
    expect(everything).not.toContain(SECRET)
    expect(everything).not.toContain('acme')
  })
})

describe('parseUtc', () => {
  it('treats a Z timestamp as UTC', () => {
    expect(parseUtc('2026-09-21T10:30:00Z').getTime()).toBe(Date.UTC(2026, 8, 21, 10, 30, 0))
  })

  it('treats a timestamp with no zone as UTC (the /api/runs shape)', () => {
    expect(parseUtc('2026-09-21T10:30:00').getTime()).toBe(Date.UTC(2026, 8, 21, 10, 30, 0))
    expect(parseUtc('2026-09-21T10:30:00.250000').getTime()).toBe(Date.UTC(2026, 8, 21, 10, 30, 0, 250))
  })

  it('keeps an explicit offset', () => {
    expect(parseUtc('2026-09-21T12:30:00+02:00').getTime()).toBe(Date.UTC(2026, 8, 21, 10, 30, 0))
  })

  it('returns an invalid Date for text that is not a timestamp', () => {
    expect(Number.isNaN(parseUtc('not a date').getTime())).toBe(true)
  })
})
