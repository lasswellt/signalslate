import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import PackageAddDialog from '~/components/PackageAddDialog.vue'
import type { PackageOut } from '~/composables/usePackagesApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function pkg(overrides: Partial<PackageOut> = {}): PackageOut {
  return {
    id: 1,
    carrier: 'ups',
    tracking_number: '1Z999AA10123456784',
    merchant: null,
    order_id: null,
    status: 'pending',
    status_source: 'manual',
    eta: null,
    delivered_at: null,
    last_event_at: null,
    last_checked_at: null,
    last_error: null,
    archived: false,
    tracking_url: 'https://www.ups.com/track?tracknum=1Z999AA10123456784',
    last_event: null,
    ...overrides,
  }
}

let postResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'POST' && path === '/api/packages') {
        const result = postResult()
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

async function typeNumber(value: string) {
  const input = $('package-add-number')?.querySelector('input') ?? $<HTMLInputElement>('package-add-number')
  if (!input) throw new Error('no number input')
  ;(input as HTMLInputElement).value = value
  input.dispatchEvent(new Event('input'))
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountDialog() {
  const wrapper = await mountSuspended(PackageAddDialog, { props: { modelValue: true } })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  postResult = () => pkg()
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('PackageAddDialog', () => {
  it('auto-detect posts without carrier, shows the detected carrier and emits added', async () => {
    const wrapper = await mountDialog()
    await typeNumber('1Z999AA10123456784')
    await click('package-add-submit')

    expect(calls).toHaveLength(1)
    expect(calls[0].body).toEqual({ tracking_number: '1Z999AA10123456784' })
    expect($('package-add-result')?.textContent).toContain('UPS')
    expect($('package-add-result')?.textContent).toContain('1Z999AA10123456784')
    const link = $('package-add-track-link')
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(wrapper.emitted('added')?.[0]).toEqual([pkg()])
  })

  it('sends an explicitly chosen carrier', async () => {
    const wrapper = await mountDialog()
    await typeNumber('123456789012')
    ;(wrapper.vm as unknown as { carrier: string | null }).carrier = 'fedex'
    await flushPromises()
    await click('package-add-submit')

    expect(calls[0].body).toEqual({ tracking_number: '123456789012', carrier: 'fedex' })
  })

  it('shows the server message on 422 unrecognized_tracking_number', async () => {
    postResult = () =>
      apiFailure(422, { code: 'unrecognized_tracking_number', message: 'Tracking number not recognised; specify the carrier' })
    await mountDialog()
    await typeNumber('ABC123')
    await click('package-add-submit')

    expect($('package-add-error')?.textContent).toContain('Tracking number not recognised')
    expect($('package-add-result')).toBeNull()
  })

  it('shows "Already tracked" on 409 package_exists', async () => {
    postResult = () => apiFailure(409, { code: 'package_exists', message: 'Package already exists' })
    await mountDialog()
    await typeNumber('1Z999AA10123456784')
    await click('package-add-submit')

    expect($('package-add-error')?.textContent).toContain('Already tracked')
  })

  it('strips spaces from the tracking number before submit', async () => {
    await mountDialog()
    await typeNumber('  1Z 999 AA1 0123 456 784 ')
    await click('package-add-submit')

    expect(calls[0].body?.tracking_number).toBe('1Z999AA10123456784')
  })

  it('disables submit when the number is empty', async () => {
    await mountDialog()
    const submit = $('package-add-submit')
    expect(submit?.hasAttribute('disabled') || submit?.classList.contains('disabled')).toBe(true)
    await click('package-add-submit')
    expect(calls).toHaveLength(0)
  })
})
