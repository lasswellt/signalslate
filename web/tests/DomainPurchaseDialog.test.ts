import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import DomainPurchaseDialog from '~/components/DomainPurchaseDialog.vue'
import type { PurchaseOut, PurchaseSettingsOut, StoredQuoteOut } from '~/composables/useDomainsApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function settings(overrides: Partial<PurchaseSettingsOut> = {}): PurchaseSettingsOut {
  return {
    enabled: true,
    max_price: '500.00',
    daily_cap: '1000.00',
    allow_premium: false,
    remaining_today: '850.00',
    ...overrides,
  }
}

function quote(overrides: Partial<StoredQuoteOut> = {}): StoredQuoteOut {
  return {
    id: 'quote-1',
    name: 'example.com',
    connection_id: 'godaddy:main',
    price: '12.00',
    renewal_price: '15.00',
    currency: 'USD',
    premium: false,
    expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
    ...overrides,
  }
}

function purchase(overrides: Partial<PurchaseOut> = {}): PurchaseOut {
  return {
    id: 1,
    quote_id: 'quote-1',
    status: 'succeeded',
    price: '12.00',
    created_at: new Date().toISOString(),
    detail: null,
    ...overrides,
  }
}

let settingsResult: () => unknown
let quoteResult: () => unknown
let purchaseResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'GET' && path === '/api/domains/purchase-settings') {
        const result = settingsResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/domains/quotes') {
        const result = quoteResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/domains/purchases') {
        const result = purchaseResult()
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
  const el = $<HTMLInputElement>(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountDialog(props: { name?: string; connectionId?: string } = {}) {
  const wrapper = await mountSuspended(DomainPurchaseDialog, {
    props: { open: true, name: props.name ?? 'example.com', connectionId: props.connectionId ?? 'godaddy:main' },
  })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  settingsResult = () => settings()
  quoteResult = () => quote()
  purchaseResult = () => purchase()
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('loading the quote', () => {
  it('shows a loading skeleton before the quote and settings resolve', async () => {
    let release: (value: unknown) => void = () => undefined
    quoteResult = () => new Promise((resolve) => { release = resolve }) as unknown
    const wrapper = await mountSuspended(DomainPurchaseDialog, {
      props: { open: true, name: 'example.com', connectionId: 'godaddy:main' },
    })
    mounted.push(wrapper as VueWrapper<unknown>)
    expect($('purchase-loading')).not.toBeNull()
    release(quote())
    await flushPromises()
    expect($('purchase-loading')).toBeNull()
  })

  it('shows an error with a retry action when loading fails', async () => {
    quoteResult = () => apiFailure(500, { message: 'boom' })
    await mountDialog()
    expect($('purchase-load-error')?.textContent).toContain('boom')

    quoteResult = () => quote()
    await click('purchase-retry')
    expect($('purchase-load-error')).toBeNull()
    expect($('purchase-quote')).not.toBeNull()
  })
})

describe('quote display', () => {
  it('shows registrar connection, first-year and renewal price, remaining cap, and no premium badge', async () => {
    await mountDialog()
    expect($('purchase-registrar')?.textContent).toContain('godaddy:main')
    expect($('purchase-first-year-price')?.textContent).toContain('12.00')
    expect($('purchase-renewal-price')?.textContent).toContain('15.00')
    expect($('purchase-remaining-cap')?.textContent).toContain('850.00')
    expect($('purchase-premium-badge')).toBeNull()
    expect($('purchase-countdown')).not.toBeNull()
  })

  it('shows a premium badge when the quote is premium', async () => {
    quoteResult = () => quote({ premium: true })
    await mountDialog()
    expect($('purchase-premium-badge')).not.toBeNull()
  })

  it('computes the total from years using price + (years - 1) * renewal', async () => {
    await mountDialog()
    expect($('purchase-total')?.textContent).toContain('12.00')

    expect($('purchase-years')).not.toBeNull()
    const wrapper = mounted[mounted.length - 1]
    // Drive the underlying v-model directly via the component instance to avoid depending on
    // QSelect's internal popup DOM.
    const select = wrapper.findComponent({ name: 'QSelect' })
    await select.vm.$emit('update:model-value', 3)
    await flushPromises()
    // price 12.00 + (3 - 1) * renewal 15.00 = 42.00
    expect($('purchase-total')?.textContent).toContain('42.00')
  })

  it('shows "Get new quote" once the quote has expired', async () => {
    quoteResult = () => quote({ expires_at: new Date(Date.now() - 1000).toISOString() })
    await mountDialog()
    expect($('purchase-expired')).not.toBeNull()
    expect($('purchase-countdown')).toBeNull()

    quoteResult = () => quote({ id: 'quote-2', expires_at: new Date(Date.now() + 5 * 60_000).toISOString() })
    await click('purchase-get-new-quote')
    expect($('purchase-expired')).toBeNull()
    expect($('purchase-countdown')).not.toBeNull()
    expect(calls.filter((call) => call.method === 'POST' && call.path === '/api/domains/quotes')).toHaveLength(2)
  })
})

describe('buy button gating', () => {
  it('is disabled until purchasing is enabled and the typed name matches, then submits exactly once', async () => {
    await mountDialog()
    const buyButton = $<HTMLButtonElement>('purchase-buy')
    expect(buyButton?.disabled).toBe(true)

    await setInput('purchase-confirm-name', 'example.com')
    expect($<HTMLButtonElement>('purchase-buy')?.disabled).toBe(false)

    // Two rapid clicks before the first request resolves must still send exactly one request.
    const el = $('purchase-buy')!
    el.click()
    el.click()
    await flushPromises()

    const purchaseCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/domains/purchases')
    expect(purchaseCalls).toHaveLength(1)
    expect(purchaseCalls[0]?.body).toEqual({ quote_id: 'quote-1', confirm_name: 'example.com', years: 1 })
  })

  it('stays disabled when purchasing is disabled, with an explanatory note', async () => {
    settingsResult = () => settings({ enabled: false })
    await mountDialog()
    await setInput('purchase-confirm-name', 'example.com')
    expect($<HTMLButtonElement>('purchase-buy')?.disabled).toBe(true)
    expect($('purchase-disabled-note')?.textContent).toContain('DOMAINS_PURCHASE_ENABLED')
  })

  it('stays disabled when the typed name does not exactly match', async () => {
    await mountDialog()
    await setInput('purchase-confirm-name', 'example.com.evil')
    expect($<HTMLButtonElement>('purchase-buy')?.disabled).toBe(true)
  })
})

describe('purchase outcomes', () => {
  it('emits purchased and shows a success message', async () => {
    const wrapper = await mountDialog()
    await setInput('purchase-confirm-name', 'example.com')
    purchaseResult = () => purchase({ status: 'succeeded' })
    await click('purchase-buy')
    expect($('purchase-result')?.textContent).toContain('purchased')
    expect(wrapper.emitted('purchased')?.[0]?.[0]).toMatchObject({ status: 'succeeded' })
  })

  it('explains that an "unknown" outcome will be reconciled on the next sync', async () => {
    await mountDialog()
    await setInput('purchase-confirm-name', 'example.com')
    purchaseResult = () => purchase({ status: 'unknown', detail: 'registrar call timed out' })
    await click('purchase-buy')
    expect($('purchase-result')?.textContent).toContain('reconciled on the next sync')
    expect($('purchase-result')?.textContent).toContain('registrar call timed out')
  })

  it('renders every PurchaseRefused reason code as readable text', async () => {
    await mountDialog()
    await setInput('purchase-confirm-name', 'example.com')
    purchaseResult = () => apiFailure(422, { code: 'daily_cap_exceeded', message: '999.00 would exceed daily cap 1000.00' })
    await click('purchase-buy')
    expect($('purchase-error')?.textContent).toContain("exceed today's daily spending cap")
    expect($('purchase-result')).toBeNull()
  })
})

describe('closing', () => {
  it('emits update:open false and closed from the cancel button', async () => {
    const wrapper = await mountDialog()
    await click('purchase-cancel')
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    expect(wrapper.emitted('closed')).toHaveLength(1)
  })
})
