import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import SourceItemsPage from '~/pages/collectors/[source].vue'
import type { ItemDetail, ItemPage, ItemRow } from '~/composables/useApi'

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString().replace(/\.\d+Z$/, 'Z')

function itemRow(id: number, overrides: Partial<ItemRow> = {}): ItemRow {
  return { id, item_type: 'message', external_id: `ext-${id}`, occurred_at: ago(60), preview: `preview ${id}`, ...overrides }
}

interface Call {
  method: string
  path: string
  params?: Record<string, unknown>
}

interface State {
  items: (params: Record<string, unknown>) => ItemPage | Error
  item: (id: number) => ItemDetail | Error
}

let calls: Call[]
let state: State

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function freshState(): State {
  return {
    items: () => ({ items: [], next_before_id: null, total: 0 }),
    item: (id) => ({ id, item_type: 'message', external_id: `ext-${id}`, occurred_at: ago(60), payload: '{}', truncated: false }),
  }
}

/** A stand-in for the network: routes by method and path, records every call. */
function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; params?: Record<string, unknown> } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, params: options.params })
      const answer = (value: unknown) => {
        if (value instanceof Error) throw value
        return value
      }
      const itemMatch = /^\/api\/collectors\/[^/]+\/items\/(\d+)$/.exec(path)
      if (method === 'GET' && itemMatch) return answer(state.item(Number(itemMatch[1])))
      if (method === 'GET' && /^\/api\/collectors\/[^/]+\/items$/.test(path)) return answer(state.items(options.params ?? {}))
      throw new Error(`unexpected ${method} ${path}`)
    },
  )
}

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)
const $$ = (testid: string) => [...body.querySelectorAll<HTMLElement>(`[data-testid="${testid}"]`)]

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(SourceItemsPage))),
})

let mounted: Array<VueWrapper<unknown>> = []

async function mountPage() {
  const wrapper = await mountSuspended(Harness, { attachTo: document.body, route: '/collectors/zoom' })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper as VueWrapper<unknown>
}

function typeSelect(wrapper: VueWrapper<unknown>) {
  const select = wrapper.findAllComponents({ name: 'QSelect' })[0]
  if (!select) throw new Error('no type filter select')
  return select
}

beforeEach(() => {
  state = freshState()
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('source items page', () => {
  it('shows the private-content notice, the item count and the empty state', async () => {
    await mountPage()
    expect($('items-notice')?.textContent).toContain('private message content')
    expect($('empty-state')).not.toBeNull()
    expect($('page-header-title')?.textContent).toContain('Zoom items')
  })

  it('loads every page of items and lists them newest first', async () => {
    state.items = (params) =>
      params.before_id === undefined
        ? { items: [itemRow(9), itemRow(8)], next_before_id: 8, total: 3 }
        : { items: [itemRow(7)], next_before_id: null, total: 3 }
    await mountPage()

    expect($$('item-row').map((row) => row.textContent)).toEqual([
      expect.stringContaining('preview 9'),
      expect.stringContaining('preview 8'),
      expect.stringContaining('preview 7'),
    ])
    const itemCalls = calls.filter((call) => call.path === '/api/collectors/zoom/items')
    expect(itemCalls).toHaveLength(2)
    expect(itemCalls[0]?.params?.before_id).toBeUndefined()
    expect(itemCalls[1]?.params?.before_id).toBe(8)
    expect($('page-header-subtitle')?.textContent).toContain('3 items')
  })

  it('filters the loaded list by item type without another fetch', async () => {
    state.items = () => ({
      items: [itemRow(9, { item_type: 'message' }), itemRow(8, { item_type: 'file', preview: 'only file' })],
      next_before_id: null,
      total: 2,
    })
    const wrapper = await mountPage()
    expect($$('item-row')).toHaveLength(2)
    const before = calls.filter((call) => call.path === '/api/collectors/zoom/items').length

    await typeSelect(wrapper).vm.$emit('update:model-value', 'file')
    await flushPromises()

    expect($$('item-row').map((row) => row.textContent)).toEqual([expect.stringContaining('only file')])
    expect(calls.filter((call) => call.path === '/api/collectors/zoom/items')).toHaveLength(before)
  })

  it('shows the error state with a retry', async () => {
    state.items = () => apiFailure(500, 'The store is unavailable')
    await mountPage()
    expect($('load-error')?.textContent).toContain('The store is unavailable')

    state.items = () => ({ items: [itemRow(1)], next_before_id: null, total: 1 })
    await click('retry')
    expect($('load-error')).toBeNull()
    expect($$('item-row')).toHaveLength(1)
  })

  it('opens item detail with humanized keys, formatted dates and no HTML execution', async () => {
    const payload = JSON.stringify(
      { subject: 'Weekly sync', sent_at: '2026-09-20T12:00:00Z', from: { name: '<img src=x onerror=alert(1)><b>Ann</b>' } },
      null,
      2,
    )
    state.items = () => ({ items: [itemRow(5, { preview: '<i>preview</i>' })], next_before_id: null, total: 1 })
    state.item = (id) => ({ id, item_type: 'message', external_id: 'ext-5', occurred_at: ago(60), payload, truncated: true })
    await mountPage()

    // The list preview itself is rendered as text, not markup.
    expect($$('item-row')[0]?.querySelector('i')).toBeNull()

    await click('item-row')
    await vi.waitFor(() => expect($$('detail-field').length).toBeGreaterThan(0))

    const fields = $$('detail-field').map((el) => el.textContent)
    expect(fields.some((text) => text?.includes('Subject: Weekly sync'))).toBe(true)
    expect(fields.some((text) => text?.startsWith('Sent at:'))).toBe(true)
    expect($('detail-truncated')).not.toBeNull()

    await click('detail-raw-toggle')
    const pre = $('detail-payload')
    expect(pre?.tagName).toBe('PRE')
    expect(pre?.textContent).toBe(payload)
    expect(pre?.querySelector('img')).toBeNull()
    expect(pre?.querySelector('b')).toBeNull()
    expect(body.querySelector('img')).toBeNull()
    expect(calls.some((call) => call.path === '/api/collectors/zoom/items/5')).toBe(true)
  })

  it('shows the API error when an item cannot be loaded', async () => {
    state.items = () => ({ items: [itemRow(5)], next_before_id: null, total: 1 })
    state.item = () => apiFailure(404, { code: 'item_not_found', message: 'Item not found' })
    await mountPage()
    await click('item-row')
    await vi.waitFor(() => expect($('detail-error')).not.toBeNull())
    expect($('detail-error')?.textContent).toContain('Item not found')
    expect($('detail-payload')).toBeNull()
  })
})
