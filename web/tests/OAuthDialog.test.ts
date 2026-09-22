import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer, useQuasar } from 'quasar'
import { useRouter } from '#imports'
import OAuthDialog from '~/components/OAuthDialog.vue'
import ConnectionsPage from '~/pages/connections.vue'
import type { ConnectionView, SystemInfo } from '~/composables/useApi'

const PASTE_ONLY: SystemInfo = {
  secret_key_configured: true,
  store_active: true,
  public_base_url_configured: false,
  web_origins: [],
  oauth: { google: { modes: ['paste_back'] }, microsoft: { modes: ['paste_back'] }, zoom: { modes: [] } },
}
const WITH_CALLBACK: SystemInfo = {
  ...PASTE_ONLY,
  public_base_url_configured: true,
  oauth: { google: { modes: ['paste_back', 'callback'] }, microsoft: { modes: ['paste_back'] }, zoom: { modes: ['callback'] } },
}

const BOTH_CALLBACK: SystemInfo = {
  ...WITH_CALLBACK,
  oauth: { google: { modes: ['paste_back', 'callback'] }, microsoft: { modes: ['paste_back', 'callback'] }, zoom: { modes: ['callback'] } },
}

// Invented values; the code must never be found anywhere it should not be.
const CODE = 'example-auth-code-4-0AbCdEf'
const PASTED = `http://127.0.0.1:8765/?state=state-1&code=${CODE}&scope=example`
const AUTH_URL = 'https://accounts.example.com/o/oauth2/auth?state=state-1'
const LEAK = 'SERVER-TEXT-THAT-MUST-NOT-SHOW'

function connection(overrides: Partial<ConnectionView> = {}): ConnectionView {
  return {
    id: 'gmail_work',
    kind: 'gmail',
    label: 'work',
    origin: 'ui',
    config: { client_id: 'cid-1', redirect_mode: 'paste_back' },
    secrets_set: ['client_secret'],
    active: false,
    health: null,
    ...overrides,
  }
}

const GMAIL = connection()
const M365 = connection({ id: 'm365_corp', kind: 'm365', label: 'corp', config: { tenant_id: 't', client_id: 'c' }, secrets_set: [] })
const ZOOM = connection({ id: 'zoom', kind: 'zoom', label: 'zoom', config: { client_id: 'cid-1', auth_mode: 'oauth' }, secrets_set: ['client_secret'] })

interface Call {
  method: string
  path: string
  credentials?: string
  body?: Record<string, unknown>
}

let calls: Call[]
let list: ConnectionView[]
let system: SystemInfo
let expiresInMs: number
let startAuthUrl: string
let pasteResult: () => unknown
let notify: ReturnType<typeof vi.fn>
let openSpy: ReturnType<typeof vi.spyOn>

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function stubApi() {
  calls = []
  vi.stubGlobal(
    '$fetch',
    async (url: string, options: { method?: string; credentials?: string; body?: string } = {}) => {
      const path = new URL(url).pathname
      const method = options.method ?? 'GET'
      calls.push({ method, path, credentials: options.credentials, body: options.body ? JSON.parse(options.body) : undefined })
      if (method === 'GET' && path === '/api/system') return system
      if (method === 'GET' && path === '/api/connections') return list
      if (method === 'POST' && path.endsWith('/start')) {
        const body = JSON.parse(options.body ?? '{}') as { mode: string }
        return {
          flow_id: 'flow-1',
          auth_url: startAuthUrl,
          mode: body.mode,
          expires_at: new Date(Date.now() + expiresInMs).toISOString().replace(/\.\d+Z$/, 'Z'),
          instructions: LEAK,
        }
      }
      if (method === 'POST' && path.endsWith('/paste')) {
        const result = pasteResult()
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

async function typePaste(value: string) {
  const el = $<HTMLInputElement>('oauth-paste')
  if (!el) throw new Error('no paste input')
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

async function submitPaste() {
  const form = body.querySelector('[data-testid="oauth-dialog"] form')
  if (!form) throw new Error('no paste form')
  form.dispatchEvent(new Event('submit', { cancelable: true }))
  await vi.waitFor(() => expect($('oauth-submit')?.classList.contains('q-btn--loading')).toBe(false))
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountDialog(conn: ConnectionView, info: SystemInfo = PASTE_ONLY) {
  const wrapper = await mountSuspended(OAuthDialog, { props: { modelValue: true, connection: conn, system: info } })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

// QPage renders nothing outside a QLayout; the harness also captures $q so Notify can be watched.
const Harness = defineComponent({
  setup() {
    const $q = useQuasar()
    const original = $q.notify.bind($q)
    notify = vi.fn((options: unknown) => original(options as never))
    $q.notify = notify as never
    return () => h(QLayout, null, () => h(QPageContainer, null, () => h(ConnectionsPage)))
  },
})

async function mountPage(route?: string) {
  const wrapper = await mountSuspended(Harness, { attachTo: document.body, ...(route ? { route } : {}) })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

async function openSignIn() {
  await click('conn-signin')
  await vi.waitFor(() => expect($('oauth-dialog')).not.toBeNull())
}

const startCalls = () => calls.filter((call) => call.path.endsWith('/start'))
const listCalls = () => calls.filter((call) => call.method === 'GET' && call.path === '/api/connections')

beforeEach(() => {
  list = [GMAIL]
  system = PASTE_ONLY
  expiresInMs = 10 * 60_000
  startAuthUrl = AUTH_URL
  pasteResult = () => ({ status: 'connected', connection: { ...GMAIL, secrets_set: ['client_secret', 'refresh_token'] }, account: null })
  stubApi()
  openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('sign-in button', () => {
  it('shows for gmail and m365 only, and only when secrets can be stored', async () => {
    list = [GMAIL, M365, connection({ id: 'slack_acme', kind: 'slack', label: 'acme', secrets_set: ['token'] })]
    await mountPage()
    expect(body.querySelectorAll('[data-testid="conn-signin"]')).toHaveLength(2)
  })

  it('is hidden without a secret key', async () => {
    system = { ...PASTE_ONLY, secret_key_configured: false }
    await mountPage()
    expect($('conn-signin')).toBeNull()
  })
})

describe('mode availability', () => {
  it('offers paste-back always and callback only when the provider lists it', async () => {
    await mountDialog(GMAIL, PASTE_ONLY)
    expect($('mode-paste_back')).not.toBeNull()
    expect($('mode-callback')).toBeNull()
    mounted.pop()?.unmount()

    await mountDialog(GMAIL, WITH_CALLBACK)
    expect($('mode-callback')).not.toBeNull()
    mounted.pop()?.unmount()

    // Microsoft is judged by its own entry, not Google's.
    await mountDialog(M365, WITH_CALLBACK)
    expect($('mode-paste_back')).not.toBeNull()
    expect($('mode-callback')).toBeNull()
  })

  it('states the Gmail 7 day rule and the Microsoft one minute rule', async () => {
    await mountDialog(GMAIL)
    expect($('oauth-gmail-note')?.textContent).toContain('In production')
    expect($('oauth-gmail-note')?.textContent).toContain('7 days')
    expect($('oauth-ms-warning')).toBeNull()
    mounted.pop()?.unmount()

    await mountDialog(M365)
    expect($('oauth-gmail-note')).toBeNull()
    await click('oauth-start')
    expect($('oauth-ms-warning')?.textContent).toContain('about a minute')
  })
})

describe('zoom', () => {
  it('offers no paste-back UI and starts with mode callback', async () => {
    await mountDialog(ZOOM, WITH_CALLBACK)
    expect($('oauth-mode')).toBeNull()
    expect($('mode-paste_back')).toBeNull()
    expect($('mode-callback')).toBeNull()
    expect($('oauth-callback-unavailable')).toBeNull()

    await click('oauth-start')
    expect(startCalls()).toEqual([
      { method: 'POST', path: '/api/oauth/zoom/start', credentials: 'include', body: { connection_id: 'zoom', mode: 'callback' } },
    ])
  })

  it('warns and disables Start when no https PUBLIC_BASE_URL makes callback unavailable', async () => {
    await mountDialog(ZOOM, PASTE_ONLY)
    expect($('oauth-callback-unavailable')).not.toBeNull()
    expect($<HTMLButtonElement>('oauth-start')?.hasAttribute('disabled')).toBe(true)

    await click('oauth-start')
    expect(startCalls()).toHaveLength(0)
  })

  it('is signed in by a stored refresh_token, like google', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    const wrapper = await mountDialog(ZOOM, WITH_CALLBACK)
    await click('oauth-start')
    list = [{ ...ZOOM, secrets_set: ['client_secret', 'refresh_token'] }]
    await vi.advanceTimersByTimeAsync(2_100)
    await flushPromises()

    expect($('oauth-connected')).not.toBeNull()
    expect(wrapper.emitted('connected')).toHaveLength(1)
    vi.useRealTimers()
  })
})

describe('start', () => {
  it('opens the provider page with noopener, sends the mode and credentials, and shows no code', async () => {
    await mountDialog(GMAIL)
    await click('oauth-start')

    expect(startCalls()).toEqual([
      { method: 'POST', path: '/api/oauth/google/start', credentials: 'include', body: { connection_id: 'gmail_work', mode: 'paste_back' } },
    ])
    expect(openSpy).toHaveBeenCalledTimes(1)
    expect(openSpy).toHaveBeenCalledWith(AUTH_URL, '_blank', 'noopener,noreferrer')
    expect($('oauth-paste')).not.toBeNull()
    expect($('oauth-countdown')?.textContent).toMatch(/Expires in \d+:\d{2}/)
    expect(body.innerHTML).not.toContain(CODE)
    // The API's own instruction text is never rendered.
    expect(body.innerHTML).not.toContain(LEAK)
  })

  it('maps a microsoft connection to the microsoft provider', async () => {
    await mountDialog(M365)
    await click('oauth-start')
    expect(startCalls()[0]?.path).toBe('/api/oauth/microsoft/start')
  })

  it.each(['http://accounts.example.com/auth', 'javascript:alert(1)', '//accounts.example.com/auth', 'data:text/html,x'])(
    'refuses to open %s',
    async (url) => {
      startAuthUrl = url
      await mountDialog(GMAIL)
      await click('oauth-start')
      expect(openSpy).not.toHaveBeenCalled()
      expect($('oauth-error')?.textContent).toContain('nothing was opened')
      expect($('oauth-paste')).toBeNull()
    },
  )
})

describe('paste-back', () => {
  it('submits the pasted address, clears the input, and keeps the code out of Notify and the DOM', async () => {
    await mountPage()
    await openSignIn()
    await click('oauth-start')
    await typePaste(PASTED)
    expect($<HTMLInputElement>('oauth-paste')?.value).toBe(PASTED)
    expect($<HTMLInputElement>('oauth-paste')?.type).toBe('password')

    await submitPaste()

    const paste = calls.find((call) => call.path === '/api/oauth/google/paste')
    expect(paste).toMatchObject({ method: 'POST', credentials: 'include', body: { flow_id: 'flow-1', url: PASTED } })
    await vi.waitFor(() => expect($('oauth-connected')).not.toBeNull())
    expect($('oauth-paste')).toBeNull()
    expect(body.innerHTML).not.toContain(CODE)
    expect(body.textContent).not.toContain(CODE)
    expect(JSON.stringify(notify.mock.calls)).not.toContain(CODE)
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ message: 'Signed in gmail_work' }))
    // The list is refreshed after the sign-in.
    expect(listCalls().length).toBeGreaterThanOrEqual(2)
  })

  it('clears the input and lets the user paste again after an unusable address', async () => {
    pasteResult = () => apiFailure(400, { code: 'invalid_pasted_url', message: LEAK })
    await mountDialog(GMAIL)
    await click('oauth-start')
    await typePaste(PASTED)
    await submitPaste()

    expect($<HTMLInputElement>('oauth-paste')?.value).toBe('')
    expect($('oauth-error')?.textContent).toContain('Copy the full address')
    expect(body.innerHTML).not.toContain(LEAK)
    expect(body.innerHTML).not.toContain(CODE)
  })

  it('returns to the start step when the flow is spent, and clears the input', async () => {
    pasteResult = () => apiFailure(410, { code: 'flow_expired', message: LEAK })
    await mountDialog(GMAIL)
    await click('oauth-start')
    await typePaste(PASTED)
    await submitPaste()

    expect($('oauth-paste')).toBeNull()
    expect($('oauth-start')).not.toBeNull()
    expect($('oauth-error')?.textContent).toContain('expired')
    expect(body.innerHTML).not.toContain(CODE)
  })

  it('shows the connected Microsoft account as text', async () => {
    list = [M365]
    pasteResult = () => ({ status: 'connected', connection: M365, account: 'user@example.com' })
    const wrapper = await mountDialog(M365)
    await click('oauth-start')
    await typePaste(PASTED)
    await submitPaste()
    expect($('oauth-connected')?.textContent).toContain('user@example.com')
    expect(wrapper.emitted('connected')).toHaveLength(1)
  })

  it('clears the pasted address when the dialog is cancelled', async () => {
    const wrapper = await mountDialog(GMAIL)
    await click('oauth-start')
    await typePaste(PASTED)
    await click('oauth-cancel')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
    await wrapper.setProps({ modelValue: false })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await click('oauth-start')
    expect($<HTMLInputElement>('oauth-paste')?.value).toBe('')
  })
})

describe('failure messages', () => {
  const cases: Array<[string, string]> = [
    ['unknown_flow', 'no longer active'],
    ['flow_expired', 'expired'],
    ['nonce_mismatch', 'This browser did not start'],
    ['provider_mismatch', 'different provider'],
    ['state_mismatch', 'different sign-in'],
    ['consent_denied', 'denied'],
    ['scope_not_granted', 'permission'],
    ['no_refresh_token', 'refresh token'],
    ['provider_error', 'provider reported an error'],
    ['exchange_failed', 'did not accept'],
    ['cache_write_failed', 'could not be saved'],
    ['secret_key_missing', 'SIGNALSLATE_SECRET_KEY'],
  ]

  it.each(cases)('maps the paste error %s to fixed text', async (code, fragment) => {
    pasteResult = () => apiFailure(400, { code, message: LEAK })
    await mountDialog(GMAIL)
    await click('oauth-start')
    await typePaste(PASTED)
    await submitPaste()
    expect($('oauth-error')?.textContent).toContain(fragment)
    expect(body.innerHTML).not.toContain(LEAK)
  })

  it('falls back to a generic message for an unknown code', async () => {
    pasteResult = () => apiFailure(500, { code: 'something_new', message: LEAK })
    await mountDialog(GMAIL)
    await click('oauth-start')
    await typePaste(PASTED)
    await submitPaste()
    expect($('oauth-error')?.textContent).toContain('Sign-in failed')
    expect(body.innerHTML).not.toContain(LEAK)
  })

  it('maps a start failure to fixed text', async () => {
    vi.stubGlobal('$fetch', async () => {
      throw apiFailure(400, { code: 'callback_unavailable', message: LEAK })
    })
    await mountDialog(GMAIL, WITH_CALLBACK)
    await click('mode-callback')
    await click('oauth-start')
    expect($('oauth-error')?.textContent).toContain('PUBLIC_BASE_URL')
    expect(body.innerHTML).not.toContain(LEAK)
    expect(openSpy).not.toHaveBeenCalled()
  })
})

describe('callback mode', () => {
  beforeEach(() => {
    // Only the interval and the clock: flushPromises and the DOM still need the real timers.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    system = WITH_CALLBACK
  })

  async function startCallback(conn = GMAIL) {
    const wrapper = await mountDialog(conn, BOTH_CALLBACK)
    await click('mode-callback')
    const before = vi.getTimerCount()
    await click('oauth-start')
    return { wrapper, before }
  }

  it('polls every 2 seconds and stops at expires_at', async () => {
    expiresInMs = 9_000
    const { before } = await startCallback()
    expect(startCalls()[0]?.body).toEqual({ connection_id: 'gmail_work', mode: 'callback' })
    expect(vi.getTimerCount()).toBeGreaterThan(before)
    expect($('oauth-paste')).toBeNull()
    const initial = listCalls().length

    await vi.advanceTimersByTimeAsync(4_100)
    expect(listCalls().length - initial).toBe(2)

    await vi.advanceTimersByTimeAsync(6_000)
    await flushPromises()
    expect($('oauth-error')?.textContent).toContain('expired')
    expect($('oauth-start')).not.toBeNull()
    expect(vi.getTimerCount()).toBe(before)

    const settled = listCalls().length
    await vi.advanceTimersByTimeAsync(20_000)
    expect(listCalls().length).toBe(settled)
  })

  it('finishes when a refresh_token appears on a gmail connection', async () => {
    const { wrapper, before } = await startCallback()
    list = [{ ...GMAIL, secrets_set: ['client_secret', 'refresh_token'] }]
    await vi.advanceTimersByTimeAsync(2_100)
    await flushPromises()

    expect($('oauth-connected')).not.toBeNull()
    expect(wrapper.emitted('connected')).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(before)
  })

  it('does not finish on an unchanged connection', async () => {
    await startCallback()
    await vi.advanceTimersByTimeAsync(6_100)
    await flushPromises()
    expect($('oauth-connected')).toBeNull()
    expect($('oauth-countdown')).not.toBeNull()
  })

  it('finishes when a microsoft connection turns healthy', async () => {
    const { wrapper, before } = await startCallback(M365)
    expect(startCalls()[0]?.path).toBe('/api/oauth/microsoft/start')

    await vi.advanceTimersByTimeAsync(2_100)
    expect(wrapper.emitted('connected')).toBeUndefined()

    list = [{ ...M365, health: { status: 'ok', detail: null, checked_at: '2026-09-21T10:00:00Z' } }]
    await vi.advanceTimersByTimeAsync(2_100)
    await flushPromises()
    expect($('oauth-connected')).not.toBeNull()
    expect(wrapper.emitted('connected')).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(before)
  })

  it('stops polling when the dialog is closed', async () => {
    const { before } = await startCallback()
    await click('oauth-cancel')
    expect(vi.getTimerCount()).toBe(before)
    const settled = listCalls().length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(listCalls().length).toBe(settled)
  })

  it('stops polling when the component unmounts', async () => {
    const { wrapper, before } = await startCallback()
    wrapper.unmount()
    mounted = mounted.filter((entry) => entry !== (wrapper as VueWrapper<unknown>))
    expect(vi.getTimerCount()).toBe(before)
    const settled = listCalls().length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(listCalls().length).toBe(settled)
  })
})

describe('landing query', () => {
  it('shows a fixed message for a known reason and then drops the query', async () => {
    await mountPage('/connections?oauth=error&reason=denied')
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ type: 'negative', message: expect.stringContaining('denied') }))
    await vi.waitFor(() => expect(useRouter().currentRoute.value.query).toEqual({}))
  })

  it('never shows an unknown reason', async () => {
    await mountPage('/connections?oauth=error&reason=%3Cb%3Eboom%3C%2Fb%3E')
    expect(JSON.stringify(notify.mock.calls)).not.toContain('boom')
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ type: 'negative', message: expect.stringContaining('Sign-in failed') }))
    await vi.waitFor(() => expect(useRouter().currentRoute.value.query).toEqual({}))
  })

  it('confirms a successful sign-in and drops the query', async () => {
    await mountPage('/connections?oauth=ok')
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ type: 'positive', message: 'Sign-in complete' }))
    await vi.waitFor(() => expect(useRouter().currentRoute.value.query).toEqual({}))
  })

  it('does nothing without the query', async () => {
    await mountPage('/connections')
    expect(notify).not.toHaveBeenCalled()
  })
})
