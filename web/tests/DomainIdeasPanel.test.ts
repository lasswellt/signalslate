import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import DomainIdeasPanel from '~/components/DomainIdeasPanel.vue'
import type { CandidateOut, QuoteOut } from '~/composables/useDomainsApi'
import type { ConnectionView } from '~/composables/useApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function connection(overrides: Partial<ConnectionView> = {}): ConnectionView {
  return {
    id: 'godaddy:main',
    kind: 'godaddy',
    label: 'Main GoDaddy',
    origin: 'ui',
    config: {},
    secrets_set: [],
    active: true,
    health: null,
    ...overrides,
  }
}

function candidates(): CandidateOut[] {
  return [
    { name: 'acmehq.com', status: 'likely_available' },
    { name: 'acmehq.net', status: 'taken' },
    { name: 'acmehq.io', status: 'unknown' },
  ]
}

function quote(overrides: Partial<QuoteOut> = {}): QuoteOut {
  return {
    name: 'acmehq.com',
    available: true,
    premium: false,
    price: '12.00',
    renewal_price: '15.00',
    currency: 'USD',
    ...overrides,
  }
}

let connectionsResult: () => unknown
let ideasResult: () => unknown
let checkResult: () => unknown
let addDomainResult: () => unknown
let settingsResult: () => unknown
let purchaseQuoteResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'GET' && path === '/api/connections') {
        const result = connectionsResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/domains/ideas') {
        const result = ideasResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/domains/check') {
        const result = checkResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/domains') {
        const result = addDomainResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'GET' && path === '/api/domains/purchase-settings') {
        const result = settingsResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/domains/quotes') {
        const result = purchaseQuoteResult()
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
  const el = $<HTMLInputElement | HTMLTextAreaElement>(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountPanel() {
  const wrapper = await mountSuspended(DomainIdeasPanel, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

/** Fills in a seed word and a TLD and clicks Generate, resolving once the results table renders. */
async function generate() {
  await setInput('ideas-seed-input', 'acmehq')
  await click('ideas-seed-add')
  await setInput('ideas-tld-input', 'com')
  await click('ideas-tld-add')
  await click('ideas-generate')
}

beforeEach(() => {
  connectionsResult = () => [connection()]
  ideasResult = () => ({ candidates: candidates(), llm_reason: null })
  checkResult = () => [quote()]
  addDomainResult = () => ({
    name: 'acmehq.com', ownership: 'watched', source: 'ui', connection_id: null, expires_at: null,
    auto_renew: null, locked: null, privacy: null, first_seen: null, last_seen: null, missing_since: null,
    mail: { status: 'unavailable', spf: false, dmarc_policy: null, dkim: false, mta_sts: false, bimi: false },
  })
  settingsResult = () => ({ enabled: true, max_price: '500.00', daily_cap: '1000.00', allow_premium: false, remaining_today: '850.00' })
  purchaseQuoteResult = () => ({ id: 'quote-1', name: 'acmehq.com', connection_id: 'godaddy:main', price: '12.00', renewal_price: '15.00', currency: 'USD', premium: false, expires_at: new Date(Date.now() + 5 * 60_000).toISOString() })
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('seeds and TLDs', () => {
  it('adds and removes seed and TLD chips, gating Generate on having at least one of each', async () => {
    await mountPanel()
    expect($<HTMLButtonElement>('ideas-generate')?.disabled).toBe(true)

    await setInput('ideas-seed-input', 'acmehq')
    await click('ideas-seed-add')
    expect($('ideas-seed-acmehq')).not.toBeNull()
    expect($<HTMLButtonElement>('ideas-generate')?.disabled).toBe(true)

    await setInput('ideas-tld-input', 'com')
    await click('ideas-tld-add')
    expect($('ideas-tld-com')).not.toBeNull()
    expect($<HTMLButtonElement>('ideas-generate')?.disabled).toBe(false)
  })
})

describe('generating candidates', () => {
  it('shows the hint before generating, then a results table with the local prefilter status', async () => {
    await mountPanel()
    expect($('ideas-hint')).not.toBeNull()

    await generate()

    const ideasCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/domains/ideas')
    expect(ideasCalls).toHaveLength(1)
    expect(ideasCalls[0]?.body).toMatchObject({ seeds: ['acmehq'], tlds: ['com'], use_llm: false })

    expect($('ideas-status-acmehq.com')?.textContent).toContain('Likely available')
    expect($('ideas-status-acmehq.net')?.textContent).toContain('Taken')
    expect($('ideas-status-acmehq.io')?.textContent).toContain('Unknown')
  })

  it('shows an empty state when no candidates come back', async () => {
    ideasResult = () => ({ candidates: [], llm_reason: null })
    await mountPanel()
    await generate()
    expect($('ideas-empty')).not.toBeNull()
  })

  it('shows an error with a retry action when generation fails', async () => {
    ideasResult = () => apiFailure(500, { message: 'boom' })
    await mountPanel()
    await generate()
    expect($('ideas-generate-error')?.textContent).toContain('boom')

    ideasResult = () => ({ candidates: candidates(), llm_reason: null })
    await click('ideas-generate-retry')
    expect($('ideas-generate-error')).toBeNull()
    expect($('ideas-status-acmehq.com')).not.toBeNull()
  })

  it('shows the llm_reason note when the API returns one', async () => {
    ideasResult = () => ({ candidates: candidates(), llm_reason: 'brief is required for LLM ideas' })
    await mountPanel()
    await generate()
    expect($('ideas-llm-note')?.textContent).toContain('brief is required for LLM ideas')
  })
})

describe('checking with a registrar', () => {
  it('checks selected candidates and shows price and premium from the quote', async () => {
    checkResult = () => [quote({ name: 'acmehq.com', price: '12.00', currency: 'USD', premium: true })]
    await mountPanel()
    await generate()

    await click('ideas-select-acmehq.com')
    const select = mounted[mounted.length - 1]!.findAllComponents({ name: 'QSelect' })[0]!
    await select.vm.$emit('update:model-value', 'godaddy:main')
    await flushPromises()
    expect($<HTMLButtonElement>('ideas-check')?.disabled).toBe(false)

    await click('ideas-check')
    const checkCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/domains/check')
    expect(checkCalls).toHaveLength(1)
    expect(checkCalls[0]?.body).toEqual({ names: ['acmehq.com'], connection_id: 'godaddy:main' })

    expect($('ideas-quote-acmehq.com')?.textContent).toContain('12.00')
    expect($('ideas-quote-acmehq.com')?.textContent).toContain('Premium')
  })

  it('shows a readable note inline when the registrar responds not_eligible', async () => {
    checkResult = () => apiFailure(409, { code: 'not_eligible', message: 'availability needs >=50 domains or $20/mo spend' })
    await mountPanel()
    await generate()

    await click('ideas-select-acmehq.com')
    const select = mounted[mounted.length - 1]!.findAllComponents({ name: 'QSelect' })[0]!
    await select.vm.$emit('update:model-value', 'godaddy:main')
    await flushPromises()

    await click('ideas-check')
    expect($('ideas-not-eligible')?.textContent).toContain('availability needs >=50 domains or $20/mo spend')
    expect($('ideas-quote-acmehq.com')?.textContent).not.toContain('12.00')
  })
})

describe('watch', () => {
  it('adds the candidate as a watched domain and shows Watched', async () => {
    await mountPanel()
    await generate()

    await click('ideas-watch-acmehq.com')
    const watchCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/domains')
    expect(watchCalls).toHaveLength(1)
    expect(watchCalls[0]?.body).toEqual({ name: 'acmehq.com', ownership: 'watched' })
    expect($('ideas-watched-acmehq.com')).not.toBeNull()
  })

  it('shows an inline error when watching fails', async () => {
    addDomainResult = () => apiFailure(409, { message: 'already tracked' })
    await mountPanel()
    await generate()

    await click('ideas-watch-acmehq.com')
    expect($('ideas-watch-error-acmehq.com')?.textContent).toContain('already tracked')
    expect($('ideas-watched-acmehq.com')).toBeNull()
  })
})

describe('buy', () => {
  it('opens the purchase dialog for the selected registrar connection', async () => {
    await mountPanel()
    await generate()

    const select = mounted[mounted.length - 1]!.findAllComponents({ name: 'QSelect' })[0]!
    await select.vm.$emit('update:model-value', 'godaddy:main')
    await flushPromises()

    await click('ideas-buy-acmehq.com')
    expect($('domain-purchase-dialog')).not.toBeNull()
    expect($('dialog-title')?.textContent).toContain('acmehq.com')

    const quoteCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/domains/quotes')
    expect(quoteCalls[0]?.body).toEqual({ name: 'acmehq.com', connection_id: 'godaddy:main' })
  })

  it('keeps Buy disabled until a registrar connection is selected', async () => {
    await mountPanel()
    await generate()
    expect($<HTMLButtonElement>('ideas-buy-acmehq.com')?.disabled).toBe(true)
  })
})
