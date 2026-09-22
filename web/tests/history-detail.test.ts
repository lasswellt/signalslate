import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import HistoryDetailPage from '~/pages/history/[id].vue'
import { parseUtc } from '~/composables/useApi'
import type { RunDetail } from '~/composables/useApi'

// The old code read a zone-less timestamp as local time, so it only differs from parseUtc when the
// process zone is not UTC. Pin one so the tests fail on the old code on any machine, CI included.
const originalTz = process.env.TZ
beforeAll(() => {
  process.env.TZ = 'America/New_York'
})
afterAll(() => {
  if (originalTz === undefined) delete process.env.TZ
  else process.env.TZ = originalTz
})

const ZONELESS = '2026-09-21T12:00:00'
const ZULU = '2026-09-21T12:00:00Z'
const GARBAGE = 'sometime last week'

function runDetail(startedAt: string): RunDetail {
  return {
    id: 7,
    trigger: 'manual',
    status: 'success',
    started_at: startedAt,
    finished_at: null,
    summary: 'summary 7',
    error: null,
    has_pdf: false,
    source_health: [{ source: 'slack_acme', status: 'ok', detail: 'fine', checked_at: ZONELESS }],
  }
}

let detail: RunDetail

beforeEach(() => {
  detail = runDetail(ZULU)
  vi.stubGlobal('$fetch', async (url: string) => {
    const path = new URL(url).pathname
    if (path === '/api/runs/7') return detail
    throw new Error(`unexpected GET ${path}`)
  })
})

let mounted: Array<VueWrapper<unknown>> = []

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

// QPage renders nothing outside a QLayout, so the page is mounted in the same shell app.vue gives it.
// The route option makes useRoute() see /history/7 without stubbing the composable.
async function mountPage() {
  const Harness = defineComponent({
    render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(HistoryDetailPage))),
  })
  const wrapper = await mountSuspended(Harness, { attachTo: document.body, route: '/history/7' })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

const pageText = () => document.body.textContent ?? ''

describe('test setup', () => {
  it('runs in a non-UTC zone where a zone-less string parsed as local time is a different instant', () => {
    expect(new Date(ZONELESS).getTimezoneOffset()).not.toBe(0)
    expect(parseUtc(ZONELESS).toLocaleString()).not.toBe(new Date(ZONELESS).toLocaleString())
  })
})

describe('run detail page', () => {
  it('renders the run id from the route and loads that run', async () => {
    await mountPage()
    expect(pageText()).toContain('Run #7')
    expect(pageText()).toContain('summary 7')
  })

  it('renders a Z-suffixed started_at as the instant parseUtc gives', async () => {
    detail = runDetail(ZULU)
    await mountPage()
    expect(pageText()).toContain(parseUtc(ZULU).toLocaleString())
  })

  it('treats a zone-less started_at as UTC, not as viewer-local time', async () => {
    detail = runDetail(ZONELESS)
    await mountPage()
    expect(pageText()).toContain(parseUtc(ZONELESS).toLocaleString())
    expect(pageText()).not.toContain(new Date(ZONELESS).toLocaleString())
  })

  it('renders an unparseable started_at as the raw text, never Invalid Date', async () => {
    detail = runDetail(GARBAGE)
    await mountPage()
    expect(pageText()).toContain(GARBAGE)
    expect(pageText()).not.toContain('Invalid Date')
  })
})
