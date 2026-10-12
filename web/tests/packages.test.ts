import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer, QSelect } from 'quasar'
import PackagesPage from '~/pages/packages.vue'
import type {
  CarrierStatus,
  PackageOut,
  PackageSummary,
  RefreshResult,
  ScanResult,
} from '~/composables/usePackagesApi'

function pkg(overrides: Partial<PackageOut> = {}): PackageOut {
  return {
    id: 1,
    carrier: 'ups',
    tracking_number: '1Z999AA10123456784',
    merchant: 'Acme',
    order_id: null,
    status: 'in_transit',
    status_source: 'carrier_api',
    eta: '2026-10-12T00:00:00Z',
    delivered_at: null,
    last_event_at: '2026-10-10T08:00:00Z',
    last_checked_at: null,
    last_error: null,
    archived: false,
    tracking_url: 'https://www.ups.com/track?tracknum=1Z999AA10123456784',
    last_event: 'Departed facility',
    ...overrides,
  }
}

const SUMMARY: PackageSummary = {
  out_for_delivery_today: 2,
  arriving_today: 3,
  delivered_today: 4,
  in_transit: 5,
  exceptions: 1,
  generated_at: '2026-10-10T09:00:00Z',
}

interface Call {
  method: string
  path: string
  query: Record<string, string>
}

interface Routes {
  packages?: PackageOut[] | Error
  carriers?: CarrierStatus[]
  scan?: ScanResult[]
  refresh?: RefreshResult
}

let calls: Call[]

/** A stand-in for the network: routes by method and path, records every call. */
function stubApi(routes: Routes) {
  calls = []
  vi.stubGlobal('$fetch', async (url: string, options: { method?: string; params?: Record<string, string> } = {}) => {
    const parsed = new URL(url)
    const path = parsed.pathname
    const method = options.method ?? 'GET'
    calls.push({ method, path, query: { ...options.params } })
    if (method === 'GET' && path === '/api/packages') {
      const value = routes.packages ?? []
      if (value instanceof Error) throw value
      return value
    }
    if (method === 'GET' && path === '/api/packages/summary') return SUMMARY
    if (method === 'GET' && path === '/api/packages/carriers') {
      return routes.carriers ?? [{ code: 'ups', configured: true }, { code: 'usps', configured: true }]
    }
    if (method === 'POST' && path === '/api/packages/scan') {
      return routes.scan ?? [{ label: 'main', scanned: 10, packages_touched: 2, errors: [] }]
    }
    if (method === 'POST' && path === '/api/packages/refresh') {
      return routes.refresh ?? { polled: 3, changed: 1, errors: {} }
    }
    throw new Error(`unexpected ${method} ${path}`)
  })
}

const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(PackagesPage))),
})

let wrapper: VueWrapper | undefined

async function mountPage(routes: Routes) {
  stubApi(routes)
  wrapper = await mountSuspended(Harness, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

const $ = (testid: string) => document.body.querySelector<HTMLElement>(`[data-testid="${testid}"]`)

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

beforeEach(() => {
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.unstubAllGlobals()
})

describe('packages page', () => {
  it('renders rows with status chips and summary numbers', async () => {
    await mountPage({
      packages: [pkg(), pkg({ id: 2, tracking_number: null, order_id: 'A-77', status: 'delivered', tracking_url: null })],
    })

    expect(document.body.querySelectorAll('[data-testid="package-row"]')).toHaveLength(2)
    const chips = [...document.body.querySelectorAll('[data-testid="status-chip"]')].map((c) => c.getAttribute('aria-label'))
    expect(chips).toEqual(['Status: In transit', 'Status: Delivered'])
    expect($('packages-table')?.textContent).toContain('Order A-77')
    expect($('packages-table')?.textContent).toContain('UPS')
    expect(document.body.querySelectorAll('[data-testid="package-track-link"]')).toHaveLength(1)

    const summary = $('packages-summary')?.textContent ?? ''
    for (const label of ['Out for delivery today', 'Arriving today', 'Delivered today', 'In transit', 'Exceptions']) {
      expect(summary).toContain(label)
    }
    expect($('packages-summary-out-for-delivery')?.textContent).toContain('2')
    expect($('packages-summary-in-transit')?.textContent).toContain('5')
    expect($('packages-carrier-notice')).toBeNull()
  })

  it('shows the empty state when there are no packages', async () => {
    await mountPage({ packages: [] })
    expect($('packages-empty')?.textContent).toContain('No packages yet. Scan Gmail or add a tracking number.')
    expect($('packages-table')).toBeNull()
  })

  it('shows an error and retries', async () => {
    await mountPage({ packages: Object.assign(new Error('boom'), { status: 500, data: { detail: 'db down' } }) })
    expect(document.body.textContent).toContain('db down')

    stubApi({ packages: [pkg()] })
    const retry = [...document.body.querySelectorAll<HTMLElement>('button')].find((b) => /retry/i.test(b.textContent ?? ''))
    expect(retry).toBeTruthy()
    retry?.click()
    await flushPromises()
    expect(document.body.querySelectorAll('[data-testid="package-row"]')).toHaveLength(1)
  })

  it('Scan Gmail POSTs /api/packages/scan', async () => {
    await mountPage({ packages: [pkg()] })
    await click('packages-scan')
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/packages/scan')).toBe(true)
  })

  it('Refresh POSTs /api/packages/refresh', async () => {
    await mountPage({ packages: [pkg()] })
    await click('packages-refresh')
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/packages/refresh')).toBe(true)
  })

  it('passes the status filter to the list request', async () => {
    const page = await mountPage({ packages: [pkg()] })
    const select = page.findAllComponents(QSelect).find((s) => s.props('label') === 'Status')
    expect(select).toBeTruthy()
    select?.vm.$emit('update:modelValue', 'delivered')
    await flushPromises()
    const listCalls = calls.filter((c) => c.method === 'GET' && c.path === '/api/packages')
    expect(listCalls[listCalls.length - 1].query.status).toBe('delivered')
  })

  it('shows the carrier notice when a carrier is not configured', async () => {
    await mountPage({
      packages: [pkg()],
      carriers: [{ code: 'ups', configured: false }, { code: 'usps', configured: false }],
    })
    expect($('packages-carrier-notice')?.textContent).toContain('API not configured; status comes from email only')
  })

  it('ignores a stale response when list requests resolve out of order', async () => {
    const page = await mountPage({ packages: [] })
    const pending: Array<(rows: PackageOut[]) => void> = []
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/packages') return new Promise<PackageOut[]>((resolve) => pending.push(resolve))
      if (path === '/api/packages/summary') return SUMMARY
      if (path === '/api/packages/carriers') return [{ code: 'ups', configured: true }]
      throw new Error(`unexpected ${path}`)
    })
    const select = page.findAllComponents(QSelect).find((s) => s.props('label') === 'Status')
    select?.vm.$emit('update:modelValue', 'delivered')
    await flushPromises()
    select?.vm.$emit('update:modelValue', 'pending')
    await flushPromises()
    expect(pending).toHaveLength(2)

    pending[1]([pkg({ id: 2, tracking_number: 'SECOND-ROW', carrier: 'usps' })])
    await flushPromises()
    pending[0]([pkg({ id: 1, tracking_number: 'FIRST-ROW' })])
    await flushPromises()

    const text = $('packages-table')?.textContent ?? ''
    expect(text).toContain('SECOND-ROW')
    expect(text).not.toContain('FIRST-ROW')
  })

  it('Enter on the track link does not open the detail dialog', async () => {
    await mountPage({ packages: [pkg()] })
    const link = $('package-track-link')
    expect(link).toBeTruthy()
    const init = { key: 'Enter', keyCode: 13, bubbles: true, cancelable: true }
    link?.dispatchEvent(new KeyboardEvent('keydown', init))
    link?.dispatchEvent(new KeyboardEvent('keyup', init))
    await flushPromises()
    expect($('package-detail-dialog')).toBeNull()
  })
})
