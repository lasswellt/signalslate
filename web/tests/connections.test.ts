import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer } from 'quasar'
import ConnectionsPage from '~/pages/connections.vue'
import type { ConnectionView, DigestConfig, SystemInfo } from '~/composables/useApi'

const SYSTEM: SystemInfo = {
  secret_key_configured: true,
  store_active: true,
  public_base_url_configured: false,
  web_origins: [],
  oauth: { google: { modes: ['paste_back'] }, microsoft: { modes: ['paste_back'] }, zoom: { modes: ['callback'] } },
}

function connection(overrides: Partial<ConnectionView> = {}): ConnectionView {
  return {
    id: 'slack_acme',
    kind: 'slack',
    label: 'acme',
    origin: 'ui',
    config: {},
    secrets_set: ['token'],
    active: false,
    health: { status: 'ok', detail: 'Authenticated as example-bot', checked_at: new Date(Date.now() - 5 * 60_000).toISOString().replace(/\.\d+Z$/, 'Z') },
    ...overrides,
  }
}

interface Call {
  method: string
  path: string
  headers?: Record<string, string>
  body?: unknown
}

interface Routes {
  system?: SystemInfo | Error
  connections?: ConnectionView[] | Error
  config?: DigestConfig
  putConfig?: (body: DigestConfig) => DigestConfig | Error
  test?: { status: 'ok' | 'error'; detail: string } | Error
}

let calls: Call[]

/** A stand-in for the network: routes by method and path, records every call. */
function stubApi(routes: Routes) {
  calls = []
  vi.stubGlobal('$fetch', async (url: string, options: { method?: string; headers?: Record<string, string>; body?: string } = {}) => {
    const path = new URL(url).pathname
    const method = options.method ?? 'GET'
    calls.push({ method, path, headers: options.headers, body: options.body ? JSON.parse(options.body) : undefined })
    const answer = (value: unknown) => {
      if (value instanceof Error) throw value
      return value
    }
    if (method === 'GET' && path === '/api/system') return answer(routes.system ?? SYSTEM)
    if (method === 'GET' && path === '/api/connections') return answer(routes.connections ?? [])
    if (method === 'GET' && path === '/api/config') return routes.config ?? { schedule_cron: '0 6 * * *', tracker: 'none', active_sources: {} }
    if (method === 'PUT' && path === '/api/config') {
      const body = JSON.parse(options.body ?? '{}') as DigestConfig
      return answer(routes.putConfig ? routes.putConfig(body) : body)
    }
    if (method === 'POST' && path.endsWith('/test')) return answer(routes.test ?? { status: 'ok', detail: 'fine' })
    throw new Error(`unexpected ${method} ${path}`)
  })
}

function apiFailure(status: number, detail: string): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

// QPage renders nothing outside a QLayout, so the page is mounted in the same shell app.vue gives it.
const Harness = defineComponent({
  render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(ConnectionsPage))),
})

async function mountPage() {
  const wrapper = await mountSuspended(Harness)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  stubApi({})
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('connections page', () => {
  it('renders one card per connection, grouped by kind, with label, origin, health and a credentials summary', async () => {
    stubApi({
      connections: [
        connection(),
        connection({
          id: 'zoom',
          kind: 'zoom',
          label: 'zoom',
          origin: 'env',
          secrets_set: ['client_secret'],
          health: { status: 'error', detail: 'Token request rejected', checked_at: null },
        }),
        connection({ id: 'gmail_work', kind: 'gmail', label: 'work', secrets_set: [], health: null }),
      ],
    })
    const wrapper = await mountPage()

    // Group order follows the fixed kind order (m365, zoom, slack, gmail, ...), not insertion order.
    expect(wrapper.findAll('[data-testid="kind-group-title"]').map((el) => el.text())).toEqual(['Zoom', 'Slack', 'Gmail'])

    const cards = wrapper.findAll('[data-testid="connection"]')
    expect(cards).toHaveLength(3)
    const [zoom, slack, gmail] = cards

    expect(slack.get('[data-testid="conn-label"]').text()).toBe('acme')
    expect(slack.get('[data-testid="conn-id"]').text()).toBe('slack_acme')
    expect(slack.get('[data-testid="conn-origin"]').text()).toBe('Added here')
    expect(slack.get('[data-testid="conn-health"]').text()).toContain('Authenticated as example-bot')
    expect(slack.get('[data-testid="conn-health"]').text()).toContain('5 min ago')
    expect(slack.get('[data-testid="conn-health"] [data-testid="status-chip"]').classes()).toContain('bg-positive')
    expect(slack.get('[data-testid="conn-secrets"]').text()).toBe('Credentials saved: Token')

    expect(zoom.get('[data-testid="conn-origin"]').text()).toBe('Set in .env')
    expect(zoom.get('[data-testid="conn-health"]').text()).toContain('Token request rejected')
    expect(zoom.get('[data-testid="conn-health"] [data-testid="status-chip"]').classes()).toContain('bg-negative')
    expect(zoom.get('[data-testid="conn-secrets"]').text()).toBe('Credentials saved: Client secret')

    expect(gmail.get('[data-testid="conn-health"]').text()).toContain('Not checked yet')
    expect(gmail.get('[data-testid="conn-health"] [data-testid="status-chip"]').classes()).toContain('bg-grey')
    expect(gmail.find('[data-testid="conn-secrets"]').exists()).toBe(false)
  })

  it('renders server strings as text, never as markup', async () => {
    stubApi({
      connections: [
        connection({ health: { status: 'error', detail: '<img src=x onerror=alert(1)><b>bold</b>', checked_at: null } }),
      ],
    })
    const wrapper = await mountPage()

    const health = wrapper.get('[data-testid="conn-health"]')
    expect(health.text()).toContain('<img src=x onerror=alert(1)><b>bold</b>')
    expect(health.find('img').exists()).toBe(false)
    expect(health.find('b').exists()).toBe(false)
  })

  it('shows the key banner, with plain-language copy and the env-var instruction tucked in a detail', async () => {
    stubApi({ system: { ...SYSTEM, secret_key_configured: false }, connections: [connection({ origin: 'env' })] })
    const wrapper = await mountPage()

    const banner = wrapper.get('[data-testid="key-banner"]')
    expect(banner.text()).toContain('one-time setup')
    expect(banner.text()).not.toContain('watermark')
    expect(banner.get('code').text()).toBe('SIGNALSLATE_SECRET_KEY')
    // Env connections stay listed: env-only mode keeps working.
    expect(wrapper.findAll('[data-testid="connection"]')).toHaveLength(1)
  })

  it('shows no key banner when the key is configured', async () => {
    const wrapper = await mountPage()
    expect(wrapper.find('[data-testid="key-banner"]').exists()).toBe(false)
  })

  it('shows the empty state', async () => {
    const wrapper = await mountPage()
    expect(wrapper.get('[data-testid="empty-state-title"]').text()).toBe('No accounts yet')
    expect(wrapper.get('[data-testid="empty-state-message"]').text()).toBe('Add an account to start collecting from it.')
    expect(wrapper.find('[data-testid="connection"]').exists()).toBe(false)
  })

  it('shows a loading state until the requests settle', async () => {
    let release: (value: ConnectionView[]) => void = () => {}
    const gate = new Promise<ConnectionView[]>((resolve) => (release = resolve))
    vi.stubGlobal('$fetch', async (url: string) => {
      const path = new URL(url).pathname
      if (path === '/api/system') return SYSTEM
      return gate
    })
    const wrapper = await mountSuspended(Harness)
    await flushPromises()
    expect(wrapper.find('[data-testid="loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(false)

    release([connection()])
    await flushPromises()
    expect(wrapper.find('[data-testid="loading"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="connection"]')).toHaveLength(1)
  })

  it('shows the API error text and retries', async () => {
    stubApi({ connections: apiFailure(500, 'The store is unavailable') })
    const wrapper = await mountPage()

    expect(wrapper.get('[data-testid="load-error"]').text()).toContain('The store is unavailable')
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(false)

    stubApi({ connections: [connection()] })
    await wrapper.get('[data-testid="retry"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="load-error"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="connection"]')).toHaveLength(1)
  })

  it('Test posts with the CSRF header and shows the result inline only, with no duplicate toast', async () => {
    stubApi({
      connections: [connection()],
      test: { status: 'error', detail: 'Slack rejected the token' },
    })
    const wrapper = await mountPage()

    await wrapper.get('[data-testid="conn-test"]').trigger('click')
    await flushPromises()

    const post = calls.find((call) => call.method === 'POST')
    expect(post?.path).toBe('/api/connections/slack_acme/test')
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    const result = wrapper.get('[data-testid="test-result"]')
    expect(result.text()).toContain('Slack rejected the token')
    expect(result.get('[data-testid="status-chip"]').attributes('aria-label')).toBe('Status: Error')
    expect(document.body.textContent).not.toContain('slack_acme: Slack rejected the token')
  })

  it('Test shows an ok result and reports a failed call as an error result, with a toast for the failed call', async () => {
    stubApi({ connections: [connection()], test: { status: 'ok', detail: 'Authenticated' } })
    const wrapper = await mountPage()
    await wrapper.get('[data-testid="conn-test"]').trigger('click')
    await flushPromises()
    let result = wrapper.get('[data-testid="test-result"]')
    expect(result.text()).toContain('Authenticated')
    expect(result.get('[data-testid="status-chip"]').attributes('aria-label')).toBe('Status: OK')

    stubApi({ connections: [connection()], test: apiFailure(404, 'Unknown connection') })
    await wrapper.get('[data-testid="conn-test"]').trigger('click')
    await flushPromises()
    result = wrapper.get('[data-testid="test-result"]')
    expect(result.text()).toContain('Unknown connection')
    expect(result.get('[data-testid="status-chip"]').attributes('aria-label')).toBe('Status: Error')
    await vi.waitFor(() => expect(document.body.textContent).toContain('Unknown connection'))
  })

  it('the active toggle reads the config, writes the merged active_sources and keeps the new state', async () => {
    stubApi({
      connections: [connection({ active: false })],
      config: { schedule_cron: '0 7 * * *', tracker: 'jira', active_sources: { zoom: true, slack_acme: false } },
    })
    const wrapper = await mountPage()
    const toggle = () => wrapper.get('[data-testid="conn-active"]')
    expect(toggle().attributes('aria-checked')).toBe('false')

    await toggle().trigger('click')
    await flushPromises()

    const sequence = calls.filter((call) => call.path === '/api/config').map((call) => call.method)
    expect(sequence).toEqual(['GET', 'PUT'])
    const put = calls.find((call) => call.method === 'PUT')
    expect(put?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(put?.body).toEqual({
      schedule_cron: '0 7 * * *',
      tracker: 'jira',
      active_sources: { zoom: true, slack_acme: true },
    })
    expect(toggle().attributes('aria-checked')).toBe('true')
    expect(wrapper.find('[data-testid="toggle-error"]').exists()).toBe(false)
  })

  it('the active toggle reverts and shows the error text when the save fails', async () => {
    stubApi({
      connections: [connection({ active: true })],
      putConfig: () => apiFailure(422, 'active_sources: unknown source'),
    })
    const wrapper = await mountPage()
    const toggle = () => wrapper.get('[data-testid="conn-active"]')
    expect(toggle().attributes('aria-checked')).toBe('true')

    await toggle().trigger('click')
    await flushPromises()

    expect(toggle().attributes('aria-checked')).toBe('true')
    expect(wrapper.get('[data-testid="toggle-error"]').text()).toBe('active_sources: unknown source')
    await vi.waitFor(() => expect(document.body.textContent).toContain('active_sources: unknown source'))
  })

  it('shows Sign in for an oauth-mode zoom connection, and hides it for a server-to-server one', async () => {
    stubApi({
      connections: [
        connection({ id: 'zoom', kind: 'zoom', label: 'zoom', config: { auth_mode: 'oauth' } }),
        connection({ id: 'zoom_s2s', kind: 'zoom', label: 'zoom_s2s', config: { account_id: 'acct-1' } }),
      ],
    })
    const wrapper = await mountPage()

    const cards = wrapper.findAll('[data-testid="connection"]')
    expect(cards[0]?.find('[data-testid="conn-signin"]').exists()).toBe(true)
    expect(cards[1]?.find('[data-testid="conn-signin"]').exists()).toBe(false)
  })

  it('hides Edit and Delete together when the secret key is not configured', async () => {
    stubApi({ system: { ...SYSTEM, secret_key_configured: false }, connections: [connection({ origin: 'env' })] })
    const wrapper = await mountPage()

    expect(wrapper.find('[data-testid="conn-menu"]').exists()).toBe(false)
  })
})
