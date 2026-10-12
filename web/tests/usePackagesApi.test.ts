import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '~/composables/useApi'
import { usePackagesApi } from '~/composables/usePackagesApi'

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

type Client = ReturnType<typeof usePackagesApi>
type Call = [string, string, string, (client: Client) => Promise<unknown>]

describe('usePackagesApi', () => {
  const api = () => usePackagesApi()
  const writes: Call[] = [
    ['addPackage', 'POST', '/api/packages', (c) => c.addPackage({ tracking_number: '1Z999AA10123456784', carrier: 'ups' })],
    ['setArchived', 'PATCH', '/api/packages/7', (c) => c.setArchived(7, true)],
    ['deletePackage', 'DELETE', '/api/packages/7', (c) => c.deletePackage(7)],
    ['scan', 'POST', '/api/packages/scan', (c) => c.scan()],
    ['refresh', 'POST', '/api/packages/refresh', (c) => c.refresh()],
  ]
  const reads: Call[] = [
    ['listPackages', 'GET', '/api/packages', (c) => c.listPackages()],
    ['getPackage', 'GET', '/api/packages/7', (c) => c.getPackage(7)],
    ['summary', 'GET', '/api/packages/summary', (c) => c.summary()],
    ['carriers', 'GET', '/api/packages/carriers', (c) => c.carriers()],
  ]

  it.each(writes)('%s sends the CSRF header and a JSON content type', async (_name, method, path, call) => {
    await call(api())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, options] = fetchMock.mock.calls[0] as [string, { method: string; headers: Record<string, string> }]
    expect(url.endsWith(path)).toBe(true)
    expect(options.method).toBe(method)
    expect(options.headers['X-Requested-With']).toBe('signalslate')
    expect(options.headers['Content-Type']).toBe('application/json')
  })

  it.each(reads)('%s is a header-less GET on the right path', async (_name, method, path, call) => {
    await call(api())
    const [url, options] = fetchMock.mock.calls[0] as [string, { method: string; headers?: unknown }]
    expect(url.endsWith(path)).toBe(true)
    expect(options.method).toBe(method)
    expect(options.headers).toBeUndefined()
  })

  it('sends bodies as JSON strings', async () => {
    const client = api()
    await client.addPackage({ tracking_number: 'abc', carrier: 'ups' })
    await client.setArchived(7, false)
    const [, add] = fetchMock.mock.calls[0] as [string, { body?: string }]
    const [, patch] = fetchMock.mock.calls[1] as [string, { body?: string }]
    expect(JSON.parse(add.body ?? '')).toEqual({ tracking_number: 'abc', carrier: 'ups' })
    expect(JSON.parse(patch.body ?? '')).toEqual({ archived: false })
  })

  it('sends no body for scan and refresh', async () => {
    const client = api()
    await client.scan()
    await client.refresh()
    for (const [, options] of fetchMock.mock.calls as Array<[string, { body?: string }]>) {
      expect(options.body).toBeUndefined()
    }
  })

  it('passes list filters and drops undefined ones', async () => {
    const client = api()
    await client.listPackages({ status: 'in_transit', include_archived: true, q: 'acme' })
    await client.listPackages()
    const [, filtered] = fetchMock.mock.calls[0] as [string, { params: unknown }]
    const [, bare] = fetchMock.mock.calls[1] as [string, { params: unknown }]
    expect(filtered.params).toEqual({ status: 'in_transit', include_archived: true, q: 'acme' })
    expect(bare.params).toEqual({})
  })

  describe('error normalization', () => {
    function fetchError(status: number, data: unknown): Error {
      return Object.assign(new Error(`[POST] "http://api.example.com/x": ${status}`), { status, statusCode: status, data })
    }

    it('maps a coded detail to ApiError code and message', async () => {
      fetchMock.mockRejectedValue(fetchError(409, { detail: { code: 'package_exists', message: 'Package already tracked' } }))
      const error = await failure(() => api().addPackage({ tracking_number: 'abc' }))
      expect(error).toMatchObject({ status: 409, code: 'package_exists', message: 'Package already tracked' })
    })

    it('maps a coded scan failure', async () => {
      fetchMock.mockRejectedValue(fetchError(502, { detail: { code: 'scan_failed', message: 'Scan failed (OSError)' } }))
      const error = await failure(() => api().scan())
      expect(error).toMatchObject({ status: 502, code: 'scan_failed' })
    })

    it('falls back to a generic message when the API is unreachable', async () => {
      fetchMock.mockRejectedValue(new Error('network down'))
      const error = await failure(() => api().summary())
      expect(error).toMatchObject({ status: 0, message: 'The API could not be reached' })
    })
  })
})
