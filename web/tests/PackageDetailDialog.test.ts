import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import PackageDetailDialog from '~/components/PackageDetailDialog.vue'
import type { PackageDetailOut } from '~/composables/usePackagesApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function detail(overrides: Partial<PackageDetailOut> = {}): PackageDetailOut {
  return {
    id: 7,
    carrier: 'ups',
    tracking_number: '1Z999AA10123456784',
    merchant: 'Acme',
    order_id: null,
    status: 'in_transit',
    status_source: 'carrier',
    eta: '2026-10-12T00:00:00Z',
    delivered_at: null,
    last_event_at: '2026-10-10T08:00:00Z',
    last_checked_at: '2026-10-10T09:00:00Z',
    last_error: null,
    archived: false,
    tracking_url: 'https://www.ups.com/track?tracknum=1Z999AA10123456784',
    last_event: 'Departed facility',
    events: [
      { occurred_at: '2026-10-08T08:00:00Z', status: 'pending', description: 'Label created', location: null, source: 'carrier' },
      { occurred_at: '2026-10-10T08:00:00Z', status: 'in_transit', description: 'Departed facility', location: 'Louisville, KY', source: 'carrier' },
    ],
    emails: [
      { account_label: 'main', subject: 'Your order shipped', sender: 'ship@acme.example', received_at: '2026-10-08T07:00:00Z' },
    ],
    ...overrides,
  }
}

let getResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'GET' && path === '/api/packages/7') {
        const result = getResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'PATCH' && path === '/api/packages/7') return detail({ archived: true })
      if (method === 'DELETE' && path === '/api/packages/7') return undefined
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

async function mountDialog() {
  const wrapper = await mountSuspended(PackageDetailDialog, { props: { modelValue: true, packageId: 7 } })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  getResult = () => detail()
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('loaded package', () => {
  it('renders events newest first and source emails', async () => {
    await mountDialog()
    const events = Array.from(body.querySelectorAll('[data-testid="package-event"]')).map((el) => el.textContent ?? '')
    expect(events).toHaveLength(2)
    expect(events[0]).toContain('Departed facility')
    expect(events[1]).toContain('Label created')
    expect($('package-emails')?.textContent).toContain('Your order shipped')
    expect($('package-emails')?.textContent).toContain('ship@acme.example')
    expect($('package-events-empty')).toBeNull()
    expect($('package-track-link')?.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('shows the empty events state', async () => {
    getResult = () => detail({ events: [] })
    await mountDialog()
    expect($('package-events-empty')?.textContent).toContain('No carrier events yet')
  })

  it('hides the track link when tracking_url is null', async () => {
    getResult = () => detail({ tracking_url: null })
    await mountDialog()
    expect($('package-summary')).not.toBeNull()
    expect($('package-track-link')).toBeNull()
  })
})

describe('actions', () => {
  it('archives via PATCH {archived:true} and emits changed', async () => {
    const wrapper = await mountDialog()
    await click('package-archive')
    const patch = calls.find((call) => call.method === 'PATCH')
    expect(patch?.path).toBe('/api/packages/7')
    expect(patch?.body).toEqual({ archived: true })
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('requires a confirm step before deleting', async () => {
    const wrapper = await mountDialog()
    await click('package-delete')
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)
    await click('package-delete-confirm-btn')
    expect(calls.some((call) => call.method === 'DELETE' && call.path === '/api/packages/7')).toBe(true)
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })
})

describe('loading and errors', () => {
  it('shows an error with retry on a 500', async () => {
    getResult = () => apiFailure(500, { message: 'boom' })
    await mountDialog()
    expect($('package-detail-error')?.textContent).toContain('boom')

    getResult = () => detail()
    await click('package-detail-retry')
    expect($('package-detail-error')).toBeNull()
    expect($('package-events')).not.toBeNull()
  })
})
