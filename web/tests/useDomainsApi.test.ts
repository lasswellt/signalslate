import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '~/composables/useApi'
import { useDomainsApi } from '~/composables/useDomainsApi'

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

describe('useDomainsApi', () => {
  const api = () => useDomainsApi()
  const cases: Array<[string, string, string, (client: ReturnType<typeof useDomainsApi>) => Promise<unknown>]> = [
    ['addDomain', 'POST', '/api/domains', (c) => c.addDomain({ name: 'example.com', ownership: 'owned' })],
    ['deleteDomain', 'DELETE', '/api/domains/example.com', (c) => c.deleteDomain('example.com')],
    ['importDomains', 'POST', '/api/domains/import', (c) => c.importDomains({ csv: 'example.com\n' })],
    ['syncDomains', 'POST', '/api/domains/sync', (c) => c.syncDomains()],
    ['inspectDomain', 'POST', '/api/domains/inspect', (c) => c.inspectDomain({ name: 'example.com', intel: true })],
    [
      'generateIdeas',
      'POST',
      '/api/domains/ideas',
      (c) => c.generateIdeas({ seeds: ['acme'], tlds: ['com'], use_llm: false }),
    ],
    [
      'checkDomains',
      'POST',
      '/api/domains/check',
      (c) => c.checkDomains({ names: ['example.com'], connection_id: 'namecheap_main' }),
    ],
    [
      'createQuote',
      'POST',
      '/api/domains/quotes',
      (c) => c.createQuote({ name: 'example.com', connection_id: 'namecheap_main' }),
    ],
    [
      'createPurchase',
      'POST',
      '/api/domains/purchases',
      (c) => c.createPurchase({ quote_id: 'q1', confirm_name: 'example.com', years: 1 }),
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
    await api().createPurchase({ quote_id: 'q1', confirm_name: 'example.com', years: 2 })
    const [, options] = fetchMock.mock.calls[0] as [string, { body?: string }]
    expect(JSON.parse(options.body ?? '')).toEqual({ quote_id: 'q1', confirm_name: 'example.com', years: 2 })
  })

  it('sends no body for a body-less POST', async () => {
    await api().syncDomains()
    const [, options] = fetchMock.mock.calls[0] as [string, { body?: string }]
    expect(options.body).toBeUndefined()
  })

  it('does not send the header on reads, and keeps the query params', async () => {
    const client = api()
    await client.listDomains({ ownership: 'owned', source: 'namecheap' })
    await client.getDomain('example.com')
    await client.listPurchases()
    await client.getPurchaseSettings()
    for (const [, options] of fetchMock.mock.calls as Array<[string, { headers?: unknown; method: string }]>) {
      expect(options.method).toBe('GET')
      expect(options.headers).toBeUndefined()
    }
    const [listUrl, listOptions] = fetchMock.mock.calls[0] as [string, { params: unknown }]
    expect(listUrl.endsWith('/api/domains')).toBe(true)
    expect(listOptions.params).toEqual({ ownership: 'owned', source: 'namecheap' })
    expect((fetchMock.mock.calls[1] as [string])[0].endsWith('/api/domains/example.com')).toBe(true)
    expect((fetchMock.mock.calls[2] as [string])[0].endsWith('/api/domains/purchases')).toBe(true)
    expect((fetchMock.mock.calls[3] as [string])[0].endsWith('/api/domains/purchase-settings')).toBe(true)
  })

  it('drops undefined query params', async () => {
    await api().listDomains()
    const [, options] = fetchMock.mock.calls[0] as [string, { params: unknown }]
    expect(options.params).toEqual({})
  })

  it('escapes path segments', async () => {
    await api().getDomain('a/b c')
    expect((fetchMock.mock.calls[0] as [string])[0].endsWith('/api/domains/a%2Fb%20c')).toBe(true)
  })

  describe('error normalization', () => {
    function fetchError(status: number, data: unknown): Error {
      return Object.assign(new Error(`[POST] "http://api.example.com/x": ${status}`), { status, statusCode: status, data })
    }

    it('reads a coded PurchaseRefused detail as code and message', async () => {
      fetchMock.mockRejectedValue(
        fetchError(422, { detail: { code: 'quote_expired', message: 'quote has expired; request a new one' } }),
      )
      const error = await failure(() =>
        api().createPurchase({ quote_id: 'q1', confirm_name: 'example.com', years: 1 }),
      )
      expect(error).toMatchObject({ status: 422, code: 'quote_expired', message: 'quote has expired; request a new one' })
    })

    it('reads a plain string detail (domain_not_found, registrar_owned)', async () => {
      fetchMock.mockRejectedValue(fetchError(404, { detail: 'Domain not found' }))
      const error = await failure(() => api().getDomain('nope.com'))
      expect(error).toMatchObject({ status: 404, message: 'Domain not found' })
    })
  })
})
