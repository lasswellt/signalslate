import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import JobCompaniesPanel from '~/components/JobCompaniesPanel.vue'
import type { BoardOut, CompanyOut } from '~/composables/useJobsApi'

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function board(overrides: Partial<BoardOut> = {}): BoardOut {
  return {
    id: 1,
    ats_kind: 'greenhouse',
    board_id: 'acme',
    resolved_by: 'pattern',
    confidence: 0.9,
    verified_at: new Date().toISOString(),
    last_polled_at: null,
    last_error: null,
    ...overrides,
  }
}

function company(overrides: Partial<CompanyOut> = {}): CompanyOut {
  return {
    id: 1,
    name: 'Acme Inc',
    domain: 'acme.com',
    source: 'manual',
    status: 'active',
    first_seen: new Date().toISOString(),
    last_seen: new Date().toISOString(),
    boards: [board()],
    ...overrides,
  }
}

let listCompaniesResult: () => unknown
let addCompanyResult: () => unknown
let importCompaniesResult: () => unknown
let importYcResult: () => unknown
let importHnResult: () => unknown
let importInboxResult: () => unknown
let rescanResult: () => unknown
let overrideResult: () => unknown
let calls: Array<{ method: string; path: string; body?: Record<string, unknown> }>

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'GET' && path === '/api/jobs/companies') {
        const result = listCompaniesResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/companies') {
        const result = addCompanyResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/companies/import') {
        const result = importCompaniesResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/companies/import-yc') {
        const result = importYcResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/companies/import-hn') {
        const result = importHnResult()
        if (result instanceof Error) throw result
        return result
      }
      if (method === 'POST' && path === '/api/jobs/companies/import-inbox') {
        const result = importInboxResult()
        if (result instanceof Error) throw result
        return result
      }
      const rescanMatch = /^\/api\/jobs\/companies\/(\d+)\/rescan$/.exec(path)
      if (method === 'POST' && rescanMatch) {
        const result = rescanResult()
        if (result instanceof Error) throw result
        return result
      }
      const overrideMatch = /^\/api\/jobs\/boards\/(\d+)$/.exec(path)
      if (method === 'PUT' && overrideMatch) {
        const result = overrideResult()
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

function buttonWithLabel(label: string): HTMLElement {
  const el = [...body.querySelectorAll<HTMLElement>('button')].find((b) => b.textContent?.trim() === label)
  if (!el) throw new Error(`no button labeled "${label}"`)
  return el
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountPanel() {
  const wrapper = await mountSuspended(JobCompaniesPanel, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  listCompaniesResult = () => [company()]
  addCompanyResult = () => company({ id: 2, name: 'New Co', domain: null, boards: [] })
  importCompaniesResult = () => ({ added: ['a.com'], rejected: [] })
  importYcResult = () => ({ added: ['yc1.com', 'yc2.com'], rejected: [] })
  importHnResult = () => ({ added: ['hn1.com'], rejected: [{ line: 3, reason: 'invalid' }] })
  importInboxResult = () => ({ added: [], rejected: [] })
  rescanResult = () => company({ boards: [board({ resolved_by: 'html', confidence: 0.5 })] })
  overrideResult = () => board({ ats_kind: 'lever', board_id: 'manual-slug', resolved_by: 'manual', confidence: 1 })
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('rendering', () => {
  it('renders the companies table with an override control per row', async () => {
    await mountPanel()
    expect($('jobs-companies-table')).not.toBeNull()
    expect($('override-1')).not.toBeNull()
  })

  it('shows resolved board info with humanized job board, found by and confidence', async () => {
    await mountPanel()
    expect($('jobs-company-1')?.textContent).toContain('Greenhouse')
    expect($('jobs-company-1')?.textContent).toContain('Pattern')
    expect($('jobs-company-confidence-1')?.textContent).toContain('High (90%)')
  })

  it('shows an unresolved indicator when the company has no board', async () => {
    listCompaniesResult = () => [company({ id: 3, boards: [] })]
    await mountPanel()
    expect($('jobs-company-unresolved-3')).not.toBeNull()
  })

  it('shows a last_error indicator when set', async () => {
    listCompaniesResult = () => [company({ boards: [board({ last_error: 'timeout resolving board' })] })]
    await mountPanel()
    expect($('jobs-company-last-error-1')?.textContent).toContain('timeout resolving board')
  })
})

describe('search', () => {
  it('filters the table by name/domain (debounced) and shows a no-match empty state', async () => {
    vi.useFakeTimers()
    listCompaniesResult = () => [company(), company({ id: 2, name: 'Other Co', domain: 'other.com' })]
    await mountPanel()

    const input = $<HTMLInputElement>('jobs-companies-search')!
    input.value = 'other'
    input.dispatchEvent(new Event('input'))
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect($('jobs-company-1')).toBeNull()
    expect($('jobs-company-2')).not.toBeNull()

    input.value = 'nomatch'
    input.dispatchEvent(new Event('input'))
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect($('empty-state')).not.toBeNull()
    vi.useRealTimers()
  })
})

describe('add company', () => {
  it('calls addCompany with the entered name and domain', async () => {
    await mountPanel()
    await click('jobs-company-add-open')
    await setInput('jobs-company-add-name', 'New Co')
    await click('jobs-company-add')

    const addCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/companies')
    expect(addCalls).toHaveLength(1)
    expect(addCalls[0]?.body).toEqual({ name: 'New Co' })
  })
})

describe('CSV import', () => {
  it('imports CSV and shows the added/rejected summary', async () => {
    await mountPanel()
    await click('jobs-companies-import')
    await click('jobs-companies-import-csv')
    await setInput('jobs-companies-csv-input', 'a.com,Acme')
    await click('jobs-companies-csv-import')

    const importCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/companies/import')
    expect(importCalls).toHaveLength(1)
    expect(importCalls[0]?.body).toEqual({ csv: 'a.com,Acme' })
    expect($('jobs-companies-csv-result')?.textContent).toContain('1 added, 0 rejected')
  })
})

describe('seed imports', () => {
  it('confirms before calling importCompaniesYc', async () => {
    await mountPanel()
    await click('jobs-companies-import')
    await click('jobs-import-yc')
    buttonWithLabel('Import').click()
    await flushPromises()

    const ycCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/companies/import-yc')
    expect(ycCalls).toHaveLength(1)
  })

  it('does not call importCompaniesHn when the confirm is cancelled', async () => {
    await mountPanel()
    await click('jobs-companies-import')
    await click('jobs-import-hn')
    buttonWithLabel('Cancel').click()
    await flushPromises()

    const hnCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/companies/import-hn')
    expect(hnCalls).toHaveLength(0)
  })

  it('calls importCompaniesInbox after confirming and shows an error notify on failure', async () => {
    importInboxResult = () => apiFailure(502, { message: 'inbox scan failed' })
    await mountPanel()
    await click('jobs-companies-import')
    await click('jobs-import-inbox')
    buttonWithLabel('Import').click()
    await flushPromises()

    const inboxCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/companies/import-inbox')
    expect(inboxCalls).toHaveLength(1)
    expect(body.textContent).toContain('inbox scan failed')
  })
})

describe('board override', () => {
  it('sends {ats_kind, board_id} to overrideBoard', async () => {
    await mountPanel()
    await click('override-1')

    // The table's own rows-per-page control is a QSelect too; the override dialog's ATS-kind
    // select is the one added most recently (mounted last, since q-dialog content only renders
    // once opened).
    const selects = mounted[mounted.length - 1]!.findAllComponents({ name: 'QSelect' })
    const select = selects[selects.length - 1]!
    await select.vm.$emit('update:model-value', 'lever')
    await flushPromises()
    await setInput('jobs-board-override-board-id', 'manual-slug')

    await click('jobs-board-override-save')

    const overrideCalls = calls.filter((call) => call.method === 'PUT' && call.path === '/api/jobs/boards/1')
    expect(overrideCalls).toHaveLength(1)
    expect(overrideCalls[0]?.body).toEqual({ ats_kind: 'lever', board_id: 'manual-slug' })
  })
})

describe('rescan', () => {
  it('calls rescanCompany with the company id and shows the updated resolution', async () => {
    await mountPanel()
    await click('jobs-company-rescan-1')

    const rescanCalls = calls.filter((call) => call.method === 'POST' && call.path === '/api/jobs/companies/1/rescan')
    expect(rescanCalls).toHaveLength(1)
    expect($('jobs-company-1')?.textContent).toContain('Html')
  })

  it('shows an error when rescan fails', async () => {
    rescanResult = () => apiFailure(504, { message: 'rescan timed out' })
    await mountPanel()
    await click('jobs-company-rescan-1')
    expect($('jobs-company-rescan-error-1')?.textContent).toContain('rescan timed out')
  })
})
