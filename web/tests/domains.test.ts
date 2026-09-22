import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import DomainsPage from '~/pages/domains.vue'
import type { DomainOut, ImportOut, PurchaseOut } from '~/composables/useDomainsApi'

function domain(overrides: Partial<DomainOut> = {}): DomainOut {
  return {
    name: 'acme.com',
    ownership: 'owned',
    source: 'namecheap',
    connection_id: 'namecheap_main',
    expires_at: '2027-01-01T00:00:00Z',
    auto_renew: true,
    locked: true,
    privacy: true,
    first_seen: '2026-01-01T00:00:00Z',
    last_seen: '2026-09-01T00:00:00Z',
    missing_since: null,
    mail: { status: 'ok', spf: true, dmarc_policy: 'reject', dkim: false, mta_sts: false, bimi: false },
    ...overrides,
  }
}

function purchase(overrides: Partial<PurchaseOut> = {}): PurchaseOut {
  return {
    id: 1,
    quote_id: 'quote-1',
    status: 'submitted',
    price: '12.00',
    created_at: '2026-09-01T00:00:00Z',
    detail: null,
    ...overrides,
  }
}

interface Call {
  method: string
  path: string
  headers?: Record<string, string>
  body?: Record<string, unknown>
}

interface Routes {
  domains?: DomainOut[] | Error
  purchases?: PurchaseOut[] | Error
  addDomain?: (body: Record<string, unknown>) => DomainOut | Error
  importDomains?: (body: Record<string, unknown>) => ImportOut | Error
  sync?: () => unknown
  getDomain?: (name: string) => unknown | Error
  inspect?: (name: string) => unknown | Error
}

let calls: Call[]

/** A stand-in for the network: routes by method and path, records every call. */
function stubApi(routes: Routes) {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; headers?: Record<string, string>; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      const body = options.body ? (JSON.parse(options.body) as Record<string, unknown>) : undefined
      calls.push({ method, path, headers: options.headers, body })
      const answer = (value: unknown) => {
        if (value instanceof Error) throw value
        return value
      }
      if (method === 'GET' && path === '/api/domains') return answer(routes.domains ?? [])
      if (method === 'GET' && path === '/api/domains/purchases') return answer(routes.purchases ?? [])
      if (method === 'POST' && path === '/api/domains') {
        return answer(
          routes.addDomain
            ? routes.addDomain(body ?? {})
            : domain({ name: body?.name as string, ownership: (body?.ownership as DomainOut['ownership']) ?? 'owned' }),
        )
      }
      if (method === 'POST' && path === '/api/domains/import') {
        return answer(routes.importDomains ? routes.importDomains(body ?? {}) : { added: [], rejected: [] })
      }
      if (method === 'POST' && path === '/api/domains/sync') {
        return answer(routes.sync ? routes.sync() : { sync: [], refresh: [] })
      }
      const detailMatch = /^\/api\/domains\/([^/]+)$/.exec(path)
      if (method === 'GET' && detailMatch) {
        const name = decodeURIComponent(detailMatch[1])
        return answer(
          routes.getDomain ? routes.getDomain(name) : { ...domain({ name }), latest: null, latest_taken_at: null, history: [] },
        )
      }
      if (method === 'POST' && path === '/api/domains/inspect') {
        const name = body?.name as string
        return answer(
          routes.inspect
            ? routes.inspect(name)
            : { name, dns: { status: 'ok', records: {} }, mail: { status: 'ok' }, rdap: { status: 'ok' }, data_hash: 'x', intel: null },
        )
      }
      throw new Error(`unexpected ${method} ${path}`)
    },
  )
}

function apiFailure(status: number, detail: string): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

// QPage renders nothing outside a QLayout, so the page is mounted in the same shell app.vue gives it.
const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(DomainsPage))),
})

// q-dialog content teleports to document.body, outside the mounted wrapper's element tree, so
// dialog interactions go through the body (mirrors tests/DomainIdeasPanel.test.ts).
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
  if (!el) throw new Error(`no input ${testid}`)
  el.value = value
  el.dispatchEvent(new Event('input'))
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

describe('domains page', () => {
  it('shows a loading state until the requests settle', async () => {
    let release: (value: DomainOut[]) => void = () => {}
    const gate = new Promise<DomainOut[]>((resolve) => (release = resolve))
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/domains/purchases') return []
      return gate
    })
    const wrapper = await mountPage()
    expect(wrapper.find('[data-testid="loading"]').exists()).toBe(true)

    release([domain()])
    await flushPromises()
    expect(wrapper.find('[data-testid="loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="portfolio-table"]').exists()).toBe(true)
  })

  it('shows the API error text and retries', async () => {
    stubApi({ domains: apiFailure(500, 'The store is unavailable') })
    const wrapper = await mountPage()

    expect(wrapper.get('[data-testid="load-error"]').text()).toContain('The store is unavailable')

    stubApi({ domains: [domain()] })
    await wrapper.get('[data-testid="load-error"] button').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="load-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="portfolio-table"]').exists()).toBe(true)
  })

  it('still shows a successfully synced portfolio when the sibling purchases fetch fails', async () => {
    // Regression: domains and purchases used to load with Promise.all, so a failure on either one
    // discarded BOTH — a real bug (api/main.py route-ordering) made GET /domains/purchases 404,
    // which made a fully synced 47-domain portfolio render as "no domains yet". They must load
    // independently, and the error must show ALONGSIDE the data that did load, not hide it.
    stubApi({ domains: [domain({ name: 'acme.com' }), domain({ name: 'beta.com' })], purchases: apiFailure(404, 'Domain not found') })
    const wrapper = await mountPage()

    expect(wrapper.get('[data-testid="load-error"]').text()).toContain('Domain not found')
    expect(wrapper.find('[data-testid="portfolio-empty"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="portfolio-table"]').text()).toContain('acme.com')
    expect(wrapper.get('[data-testid="portfolio-table"]').text()).toContain('beta.com')
  })

  it('renders the Mail column from DomainOut.mail with no per-row snapshot fetch', async () => {
    stubApi({
      domains: [
        domain({
          name: 'acme.com',
          mail: { status: 'ok', spf: true, dmarc_policy: 'reject', dkim: false, mta_sts: false, bimi: false },
        }),
        domain({ name: 'unsynced.com', mail: { status: 'unavailable', spf: false, dmarc_policy: null, dkim: false, mta_sts: false, bimi: false } }),
      ],
    })
    const wrapper = await mountPage()
    const table = wrapper.get('[data-testid="portfolio-table"]')

    expect(table.get('[data-testid="mail-badge-acme.com-spf"]').text()).toBe('SPF')
    expect(table.get('[data-testid="mail-badge-acme.com-spf"]').classes().join(' ')).toContain('bg-positive')
    expect(table.get('[data-testid="mail-badge-acme.com-dmarc"]').text()).toBe('DMARC')
    expect(table.get('[data-testid="mail-badge-acme.com-dmarc"]').classes().join(' ')).toContain('bg-positive')
    expect(table.get('[data-testid="mail-badge-acme.com-dkim"]').classes().join(' ')).toContain('bg-negative')
    expect(table.get('[data-testid="mail-badge-acme.com-mta-sts"]').classes().join(' ')).toContain('bg-negative')
    expect(table.get('[data-testid="mail-badge-acme.com-bimi"]').classes().join(' ')).toContain('bg-negative')

    // No sync/snapshot has ever run for this one: the column says so instead of guessing.
    expect(table.text()).toContain('Not synced')
    expect(table.find('[data-testid="mail-badge-unsynced.com-spf"]').exists()).toBe(false)
  })

  it('shows the portfolio empty state pointing at Connections', async () => {
    const wrapper = await mountPage()
    expect(wrapper.get('[data-testid="portfolio-empty"]').text()).toContain('Connect a registrar')
    expect(wrapper.get('[data-testid="portfolio-empty-link"]').attributes('href')).toBe('/connections')
  })

  it('renders owned domains in Portfolio and highlights expiry within 30 days', async () => {
    stubApi({
      domains: [
        domain({ name: 'soon.com', expires_at: new Date(Date.now() + 5 * 86_400_000).toISOString() }),
        domain({ name: 'later.com', expires_at: new Date(Date.now() + 400 * 86_400_000).toISOString() }),
        domain({ name: 'watched.com', ownership: 'watched' }),
      ],
    })
    const wrapper = await mountPage()

    expect(wrapper.find('[data-testid="domain-row-soon.com"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="domain-row-later.com"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="domain-row-watched.com"]').exists()).toBe(false)

    const soonRow = wrapper.get('[data-testid="domain-row-soon.com"]')
    expect(soonRow.html()).toContain('text-negative')

    const laterRow = wrapper.get('[data-testid="domain-row-later.com"]')
    expect(laterRow.html()).not.toContain('text-negative')
  })

  it('hides expired owned domains by default, unhides on toggle', async () => {
    stubApi({
      domains: [
        domain({ name: 'dead.com', expires_at: new Date(Date.now() - 400 * 86_400_000).toISOString() }),
        domain({ name: 'alive.com', expires_at: new Date(Date.now() + 400 * 86_400_000).toISOString() }),
      ],
    })
    const wrapper = await mountPage()

    expect($<HTMLInputElement>('hide-expired-toggle')?.getAttribute('aria-checked')).toBe('true')
    expect(wrapper.find('[data-testid="domain-row-dead.com"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="domain-row-alive.com"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="expired-count"]').text()).toContain('1 expired domain hidden')

    await click('hide-expired-toggle')

    expect(wrapper.find('[data-testid="domain-row-dead.com"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="domain-row-alive.com"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="expired-count"]').text()).toContain('1 expired domain shown')
  })

  it('never treats a missing expires_at as expired', async () => {
    stubApi({ domains: [domain({ name: 'unknown-expiry.com', expires_at: null })] })
    const wrapper = await mountPage()
    expect(wrapper.find('[data-testid="domain-row-unknown-expiry.com"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="expired-count"]').exists()).toBe(false)
  })

  it('shows an all-expired message with a way to reveal them, distinct from the connect-a-registrar empty state', async () => {
    stubApi({
      domains: [
        domain({ name: 'dead-one.com', expires_at: new Date(Date.now() - 10 * 86_400_000).toISOString() }),
        domain({ name: 'dead-two.com', expires_at: new Date(Date.now() - 20 * 86_400_000).toISOString() }),
      ],
    })
    const wrapper = await mountPage()

    expect(wrapper.find('[data-testid="portfolio-empty"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="portfolio-all-expired"]').text()).toContain('All 2 owned domains are expired and hidden')

    await click('show-expired-link')

    expect(wrapper.find('[data-testid="portfolio-all-expired"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="domain-row-dead-one.com"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="domain-row-dead-two.com"]').exists()).toBe(true)
  })

  it('does not apply the hide-expired filter to the Watchlist tab', async () => {
    stubApi({
      domains: [domain({ name: 'watched-expired.com', ownership: 'watched', expires_at: new Date(Date.now() - 10 * 86_400_000).toISOString() })],
    })
    const wrapper = await mountPage()
    await click('tab-watchlist')
    expect(wrapper.find('[data-testid="domain-row-watched-expired.com"]').exists()).toBe(true)
  })

  it('renders watched domains in the Watchlist tab', async () => {
    stubApi({ domains: [domain({ name: 'idea.com', ownership: 'watched', connection_id: null, source: 'manual' })] })
    const wrapper = await mountPage()

    await wrapper.get('[data-testid="tab-watchlist"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="watchlist-table"]').text()).toContain('idea.com')
  })

  it('shows the Purchases tab list and empty state', async () => {
    stubApi({ purchases: [purchase()] })
    const wrapper = await mountPage()

    await wrapper.get('[data-testid="tab-purchases"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="purchases-table"]').text()).toContain('quote-1')

    stubApi({ purchases: [] })
    const empty = await mountPage()
    await empty.get('[data-testid="tab-purchases"]').trigger('click')
    await flushPromises()
    expect(empty.get('[data-testid="purchases-empty"]').text()).toBe('No purchases yet.')
  })

  it('adds a domain with the CSRF header and reloads the list', async () => {
    stubApi({ domains: [] })
    await mountPage()

    await click('domains-add-open')
    await setInput('add-name', 'newco.com')

    stubApi({ domains: [domain({ name: 'newco.com' })] })
    await click('add-submit')

    const post = calls.find((call) => call.method === 'POST' && call.path === '/api/domains')
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(post?.body).toEqual({ name: 'newco.com', ownership: 'owned' })
    expect($('add-dialog')).toBeNull()
    expect($('domain-row-newco.com')).not.toBeNull()
  })

  it('shows the add error inline when the API rejects it', async () => {
    stubApi({ domains: [], addDomain: () => apiFailure(422, 'name: invalid domain') })
    await mountPage()

    await click('domains-add-open')
    await setInput('add-name', 'bad')
    await click('add-submit')

    expect($('add-error')?.textContent).toBe('name: invalid domain')
    expect($('add-dialog')).not.toBeNull()
  })

  it('imports a CSV and shows added/rejected results', async () => {
    const result: ImportOut = { added: ['a.com'], rejected: [{ line: 2, reason: 'invalid ownership' }] }
    stubApi({ domains: [], importDomains: () => result })
    await mountPage()

    await click('domains-import-open')
    await setInput('import-csv', 'a.com\nb.com,bogus')

    stubApi({ domains: [domain({ name: 'a.com' })], importDomains: () => result })
    await click('import-submit')

    const post = calls.find((call) => call.method === 'POST' && call.path === '/api/domains/import')
    expect(post?.body).toEqual({ csv: 'a.com\nb.com,bogus' })
    expect($('import-result')?.textContent).toContain('Added 1 domain(s)')
    expect($('import-result')?.textContent).toContain('Line 2: invalid ownership')
  })

  it('syncs now, notifies and reloads the portfolio', async () => {
    const syncResult = { sync: [{ connection_id: 'namecheap_main', kind: 'namecheap', status: 'ok', detail: 'ok', domain_count: 1 }], refresh: [] }
    stubApi({ domains: [], sync: () => syncResult })
    const wrapper = await mountPage()

    stubApi({ domains: [domain()], sync: () => syncResult })
    await click('domains-sync')

    const post = calls.find((call) => call.method === 'POST' && call.path === '/api/domains/sync')
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(wrapper.find('[data-testid="domain-row-acme.com"]').exists()).toBe(true)
  })

  it('opens the detail dialog on row click', async () => {
    stubApi({ domains: [domain()] })
    const wrapper = await mountPage()

    await wrapper.get('[data-testid="domain-row-acme.com"]').trigger('click')
    await flushPromises()

    expect($('domain-detail-dialog')).not.toBeNull()
    expect($('dialog-title')?.textContent).toBe('acme.com')
    expect(calls.some((call) => call.method === 'GET' && call.path === '/api/domains/acme.com')).toBe(true)
  })

  it('inspects an ad-hoc domain name that is not in the portfolio', async () => {
    stubApi({ domains: [], getDomain: () => apiFailure(404, 'not found') })
    await mountPage()

    await click('domains-inspect-open')
    await setInput('inspect-name', 'nottracked.com')
    await click('inspect-submit')

    expect($('inspect-dialog')).toBeNull()
    expect($('dialog-title')?.textContent).toBe('nottracked.com')
    expect($('domain-adhoc-note')).not.toBeNull()
  })
})
