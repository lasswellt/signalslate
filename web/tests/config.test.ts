import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import ConfigPage from '~/pages/config.vue'
import type { DigestConfig, StatusResponse } from '~/composables/useApi'

interface Call {
  method: string
  path: string
  headers?: Record<string, string>
  body?: unknown
}

interface State {
  config: DigestConfig
  status: StatusResponse | null
  putConfig?: (body: DigestConfig) => DigestConfig | Error
}

let calls: Call[]
let state: State

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function freshState(): State {
  return {
    config: { schedule_cron: '0 7 * * 1-5', tracker: 'mstodo', active_sources: { zoom: true, slack_acme: false } },
    status: {
      last_run: null,
      next_scheduled_run: '2026-09-29T07:00:00Z',
      source_health: [],
    },
  }
}

/** A stand-in for the network: routes by method and path, records every call. */
function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; headers?: Record<string, string>; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      const body = options.body ? JSON.parse(options.body) : undefined
      calls.push({ method, path, headers: options.headers, body })
      const answer = (value: unknown) => {
        if (value instanceof Error) throw value
        return value
      }
      if (method === 'GET' && path === '/api/config') return answer(state.config)
      if (method === 'PUT' && path === '/api/config') return answer(state.putConfig ? state.putConfig(body) : body)
      if (method === 'GET' && path === '/api/status') {
        if (state.status === null) throw apiFailure(500, 'unavailable')
        return answer(state.status)
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

async function type(testid: string, value: string) {
  const el = $<HTMLInputElement>(testid)
  if (!el) throw new Error(`no input ${testid}`)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

function buttonWithLabel(label: string): HTMLElement {
  const el = [...body.querySelectorAll<HTMLElement>('button')].find((b) => b.textContent?.trim() === label)
  if (!el) throw new Error(`no button labeled "${label}"`)
  return el
}

// QPage renders nothing outside a QLayout, so the page is mounted in the same shell app.vue gives it.
const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(ConfigPage))),
})

let mounted: Array<VueWrapper<unknown>> = []

async function mountPage() {
  const wrapper = await mountSuspended(Harness, { attachTo: document.body, route: '/config' })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper as VueWrapper<unknown>
}

function selects(wrapper: VueWrapper<unknown>) {
  const found = wrapper.findAllComponents({ name: 'QSelect' })
  return { preset: found[0]!, tracker: found[1]! }
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

describe('load', () => {
  it('shows the loading skeleton then the loaded schedule, tracker and sources', async () => {
    let release: (value: DigestConfig) => void = () => {}
    const gate = new Promise<DigestConfig>((resolve) => (release = resolve))
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/config') return gate
      if (path === '/api/status') return state.status
      throw new Error(`unexpected GET ${path}`)
    })
    await mountPage()
    expect($('loading')).not.toBeNull()

    release(state.config)
    await flushPromises()

    expect($('loading')).toBeNull()
    expect($('schedule-description')?.textContent).toContain('Weekdays at 07:00')
    expect($('schedule-next-run')?.textContent).toContain('Next run:')
    expect($('source-toggle-zoom')).not.toBeNull()
    expect($('source-toggle-slack_acme')).not.toBeNull()
    expect(body.textContent).toContain('Slack acme')
  })

  it('shows the API error and retries', async () => {
    const failing = apiFailure(500, 'The store is unavailable')
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/config') throw failing
      if (path === '/api/status') return state.status
      throw new Error(`unexpected GET ${path}`)
    })
    await mountPage()
    expect($('load-error')?.textContent).toContain('The store is unavailable')

    stubApi()
    await click('retry')
    expect($('load-error')).toBeNull()
    expect($('schedule-description')).not.toBeNull()
  })

  it('does not fail the page when /status is unavailable', async () => {
    state.status = null
    await mountPage()
    expect($('load-error')).toBeNull()
    expect($('schedule-next-run')).toBeNull()
  })
})

describe('preset -> cron mapping', () => {
  it('a known preset maps straight to its cron and description', async () => {
    const wrapper = await mountPage()
    const { preset } = selects(wrapper)
    expect(preset.props('modelValue')).toBe('weekdays')

    await preset.vm.$emit('update:model-value', 'daily')
    await flushPromises()
    expect($('schedule-description')?.textContent).toContain('Every day at 07:00')
    expect($('schedule-cron')).toBeNull()
  })

  it('an unmatched cron shows as Custom with the field visible', async () => {
    state.config = { ...state.config, schedule_cron: '15 3 * * 2,4' }
    const wrapper = await mountPage()
    const { preset } = selects(wrapper)
    expect(preset.props('modelValue')).toBe('custom')
    expect($('schedule-cron')).not.toBeNull()
    expect($<HTMLInputElement>('schedule-cron')?.value).toBe('15 3 * * 2,4')
  })
})

describe('custom cron validation', () => {
  it('disables Save on an invalid cron and enables it once valid', async () => {
    const wrapper = await mountPage()
    const { preset } = selects(wrapper)
    await preset.vm.$emit('update:model-value', 'custom')
    await flushPromises()

    await type('schedule-cron', '0 8 * *')
    expect($('save-btn')?.hasAttribute('disabled')).toBe(true)

    await type('schedule-cron', '0 8 * * *')
    expect($('save-btn')?.hasAttribute('disabled')).toBe(false)
    expect($('schedule-description')?.textContent).toContain('Every day at 08:00')
  })
})

describe('save', () => {
  it('saves successfully, notifies and clears the dirty state', async () => {
    const wrapper = await mountPage()
    const { tracker } = selects(wrapper)
    await tracker.vm.$emit('update:model-value', 'todoist')
    await flushPromises()
    expect($('save-btn')?.hasAttribute('disabled')).toBe(false)

    await click('save-btn')

    const put = calls.find((call) => call.method === 'PUT' && call.path === '/api/config')
    expect(put?.body).toMatchObject({ tracker: 'todoist' })
    expect(put?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(body.textContent).toContain('Schedule saved.')
    expect($('save-btn')?.hasAttribute('disabled')).toBe(true)
    expect($('save-btn')?.querySelector('.q-spinner')).toBeNull()
  })

  it('shows the API error and leaves Save clickable (not stuck) on failure', async () => {
    state.putConfig = () => apiFailure(422, 'schedule_cron: invalid')
    const wrapper = await mountPage()
    const { tracker } = selects(wrapper)
    await tracker.vm.$emit('update:model-value', 'todoist')
    await flushPromises()

    await click('save-btn')

    expect(body.textContent).toContain('schedule_cron: invalid')
    expect($('save-btn')?.querySelector('.q-spinner')).toBeNull()
    expect($('save-btn')?.hasAttribute('disabled')).toBe(false)
  })
})

describe('dirty guard', () => {
  it('blocks navigation until Discard is confirmed, and stays put on Keep editing', async () => {
    const wrapper = await mountPage()
    const { tracker } = selects(wrapper)
    await tracker.vm.$emit('update:model-value', 'todoist')
    await flushPromises()

    const router = (wrapper.vm as unknown as { $router: { push: (to: string) => Promise<unknown>; currentRoute: { value: { fullPath: string } } } }).$router
    const first = router.push('/collectors')
    await flushPromises()
    expect(body.textContent).toContain('Discard unsaved changes?')

    buttonWithLabel('Keep editing').click()
    await first
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/config')

    const second = router.push('/collectors')
    await flushPromises()
    expect(body.textContent).toContain('Discard unsaved changes?')
    buttonWithLabel('Discard').click()
    await second
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/collectors')
  })

  it('does not prompt when there are no unsaved changes', async () => {
    const wrapper = await mountPage()
    const router = (wrapper.vm as unknown as { $router: { push: (to: string) => Promise<unknown>; currentRoute: { value: { fullPath: string } } } }).$router
    await router.push('/collectors')
    await flushPromises()
    expect(body.textContent).not.toContain('Discard unsaved changes?')
    expect(router.currentRoute.value.fullPath).toBe('/collectors')
  })
})
