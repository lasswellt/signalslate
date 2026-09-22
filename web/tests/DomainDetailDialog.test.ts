import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import DomainDetailDialog from '~/components/DomainDetailDialog.vue'
import type { DomainDetailOut, InspectOut } from '~/composables/useDomainsApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function baseLatest(overrides: { dns?: Record<string, unknown>; mail?: Record<string, unknown>; rdap?: Record<string, unknown> } = {}): Record<string, unknown> {
  return {
    dns: {
      status: 'ok',
      error: null,
      records: {
        A: { ok: true, values: ['1.2.3.4'], error: null },
        MX: { ok: true, values: [], error: null },
      },
    },
    mail: {
      status: 'ok',
      error: null,
      spf: { present: true, record: 'v=spf1 -all', lookup_count: 1, within_limit: true, error: null },
      dmarc: { present: true, record: 'v=DMARC1; p=reject', policy: 'reject', error: null },
      dkim: { selector1: { present: true, record: 'v=DKIM1; k=rsa', error: null } },
      mta_sts: { txt_present: true, txt_record: 'v=STSv1; id=1', policy_fetched: true, policy_mode: 'enforce', error: null },
      bimi: { present: false, record: null, error: null },
      flags: [],
    },
    rdap: {
      status: 'ok',
      registrar: 'Example Registrar',
      created: '2020-01-01T00:00:00Z',
      expires: '2030-01-01T00:00:00Z',
      nameservers: ['ns1.example.com'],
      statuses: ['clientTransferProhibited'],
      locked: true,
      error: null,
    },
    ...overrides,
  }
}

function detail(overrides: Partial<DomainDetailOut> = {}): DomainDetailOut {
  return {
    name: 'example.com',
    ownership: 'owned',
    source: 'manual',
    connection_id: null,
    expires_at: null,
    auto_renew: null,
    locked: null,
    privacy: null,
    first_seen: '2026-01-01T00:00:00Z',
    last_seen: '2026-09-20T00:00:00Z',
    missing_since: null,
    latest: baseLatest(),
    latest_taken_at: '2026-09-20T00:00:00Z',
    history: [{ taken_at: '2026-09-20T00:00:00Z', data_hash: 'abcdef1234567890' }],
    ...overrides,
  }
}

function inspectOut(overrides: Partial<InspectOut> = {}): InspectOut {
  return {
    name: 'example.com',
    dns: { status: 'ok', error: null, records: {} },
    mail: { status: 'ok', error: null, spf: { present: false }, dmarc: { present: false }, dkim: {}, mta_sts: {}, bimi: { present: false }, flags: [] },
    rdap: { status: 'unsupported', error: null },
    data_hash: 'live-hash',
    intel: null,
    ...overrides,
  }
}

let getDomainResult: () => unknown
let inspectResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'GET' && path.startsWith('/api/domains/') && !path.endsWith('/inspect')) {
        const result = getDomainResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/domains/inspect') {
        const result = inspectResult()
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

async function mountDialog(name = 'example.com') {
  const wrapper = await mountSuspended(DomainDetailDialog, { props: { open: true, name } })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  getDomainResult = () => detail()
  inspectResult = () => inspectOut()
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('loading and errors', () => {
  it('shows a loading skeleton before the domain resolves', async () => {
    let release: (value: unknown) => void = () => undefined
    getDomainResult = () => new Promise((resolve) => { release = resolve }) as unknown
    const wrapper = await mountSuspended(DomainDetailDialog, { props: { open: true, name: 'example.com' } })
    mounted.push(wrapper as VueWrapper<unknown>)
    expect($('domain-detail-loading')).not.toBeNull()
    release(detail())
    await flushPromises()
    expect($('domain-detail-loading')).toBeNull()
  })

  it('shows an error with a retry action on a non-404 failure', async () => {
    getDomainResult = () => apiFailure(500, { message: 'boom' })
    await mountDialog()
    expect($('domain-detail-error')?.textContent).toContain('boom')
    expect($('domain-detail-retry')).not.toBeNull()

    getDomainResult = () => detail()
    await click('domain-detail-retry')
    expect($('domain-detail-error')).toBeNull()
    expect($('domain-dns')).not.toBeNull()
  })
})

describe('loaded domain', () => {
  it('renders DNS records, mail badges, rdap details and history', async () => {
    await mountDialog()

    expect($('domain-dns-table')).not.toBeNull()
    expect($('domain-dns')?.textContent).toContain('1.2.3.4')

    expect($('domain-mail-posture')).not.toBeNull()
    expect($('mail-badge-spf')?.textContent).toContain('SPF present')
    expect($('mail-badge-dmarc')?.textContent).toContain('DMARC reject')
    expect($('mail-badge-dkim')?.textContent).toContain('DKIM present')
    expect($('mail-badge-mta-sts')?.textContent).toContain('MTA-STS present')
    expect($('mail-badge-bimi')?.textContent).toContain('BIMI missing')
    expect($('domain-parked-warning')).toBeNull()

    expect($('domain-rdap-details')?.textContent).toContain('Example Registrar')
    expect($('domain-rdap-locked')?.textContent?.trim()).toBe('Yes')
    expect($('domain-rdap-details')?.textContent).toContain('clientTransferProhibited')

    expect($('domain-history')).not.toBeNull()
    expect($('domain-history-empty')).toBeNull()
    expect($('domain-adhoc-note')).toBeNull()
  })

  it('shows the parked-domain warning when the mail flags include it', async () => {
    getDomainResult = () => detail({
      latest: baseLatest({ mail: { status: 'ok', error: null, spf: {}, dmarc: {}, dkim: {}, mta_sts: {}, bimi: {}, flags: ['parked domain without v=spf1 -all / p=reject'] } }),
    })
    await mountDialog()
    expect($('domain-parked-warning')).not.toBeNull()
  })

  it('shows a per-part unavailable state for dns, mail and an unsupported-TLD rdap', async () => {
    getDomainResult = () => detail({
      latest: baseLatest({
        dns: { status: 'error', error: 'Timeout', records: {} },
        mail: { status: 'error', error: 'Timeout' },
        rdap: { status: 'unsupported', error: null },
      }),
    })
    await mountDialog()
    expect($('domain-dns-unavailable')?.textContent).toContain('Timeout')
    expect($('domain-mail-unavailable')?.textContent).toContain('Timeout')
    expect($('domain-rdap-unsupported')).not.toBeNull()
  })

  it('shows a not-found rdap state', async () => {
    getDomainResult = () => detail({ latest: baseLatest({ rdap: { status: 'not_found', error: null } }) })
    await mountDialog()
    expect($('domain-rdap-not-found')).not.toBeNull()
  })

  it('shows an empty history state when there are no snapshots', async () => {
    getDomainResult = () => detail({ history: [] })
    await mountDialog()
    expect($('domain-history-empty')).not.toBeNull()
  })
})

describe('ad-hoc names', () => {
  it('falls back to a live inspect lookup when the domain is not tracked, and hides history', async () => {
    getDomainResult = () => apiFailure(404, { code: 'domain_not_found', message: 'Domain not found' })
    inspectResult = () => inspectOut({
      dns: { status: 'ok', error: null, records: { A: { ok: true, values: ['9.9.9.9'], error: null } } },
      rdap: { status: 'unsupported', error: null },
    })
    await mountDialog('adhoc.example')

    expect($('domain-adhoc-note')).not.toBeNull()
    expect($('domain-history')).toBeNull()
    expect($('domain-dns')?.textContent).toContain('9.9.9.9')
    expect($('domain-rdap-unsupported')).not.toBeNull()
    expect(calls.some((call) => call.method === 'POST' && call.path === '/api/domains/inspect' && call.body?.name === 'adhoc.example')).toBe(true)
  })
})

describe('subdomains and archived URLs', () => {
  it('loads intel on demand only, and renders subdomains and archived urls', async () => {
    await mountDialog()
    expect($('domain-subdomains')).toBeNull()

    inspectResult = () => inspectOut({
      intel: {
        subdomains: { status: 'ok', names: ['www.example.com'], error: null },
        archived_urls: { status: 'ok', urls: ['https://web.archive.org/example.com'], error: null },
      },
    })
    await click('domain-load-intel')

    expect(calls.some((call) => call.method === 'POST' && call.path === '/api/domains/inspect' && call.body?.intel === true)).toBe(true)
    expect($('domain-load-intel')).toBeNull()
    expect($('domain-subdomains')?.textContent).toContain('www.example.com')
    expect($('domain-archived-urls')?.textContent).toContain('https://web.archive.org/example.com')
  })

  it('shows a per-part unavailable state when crt.sh fails', async () => {
    await mountDialog()
    inspectResult = () => inspectOut({
      intel: {
        subdomains: { status: 'error', names: [], error: 'ConnectionError' },
        archived_urls: { status: 'ok', urls: [], error: null },
      },
    })
    await click('domain-load-intel')
    expect($('domain-subdomains-unavailable')?.textContent).toContain('ConnectionError')
  })

  it('shows an error and keeps the button available to retry when the inspect call fails', async () => {
    await mountDialog()
    inspectResult = () => apiFailure(500, { message: 'inspect failed' })
    await click('domain-load-intel')
    expect($('domain-intel-error')?.textContent).toContain('inspect failed')
    expect($('domain-load-intel')).not.toBeNull()
  })
})

describe('closing', () => {
  it('emits update:open false and closed from the close button', async () => {
    const wrapper = await mountDialog()
    await click('domain-detail-close')
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    expect(wrapper.emitted('closed')).toHaveLength(1)
  })
})
