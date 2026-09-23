import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QLayout, QPageContainer, useQuasar } from 'quasar'
import ConnectionDialog from '~/components/ConnectionDialog.vue'
import ConnectionsPage from '~/pages/connections.vue'
import type { ConnectionView, SystemInfo } from '~/composables/useApi'

const SYSTEM: SystemInfo = {
  secret_key_configured: true,
  store_active: true,
  public_base_url_configured: false,
  web_origins: [],
  oauth: { google: { modes: ['paste_back'] }, microsoft: { modes: ['paste_back'] }, zoom: { modes: [] } },
}

// An invented value that must never be found anywhere it should not be.
const SECRET = 'xoxb-example-secret-value-0000'
const OTHER_SECRET = 'zoom-example-secret-value-1111'

function connection(overrides: Partial<ConnectionView> = {}): ConnectionView {
  return {
    id: 'slack_acme',
    kind: 'slack',
    label: 'acme',
    origin: 'ui',
    config: {},
    secrets_set: ['token'],
    active: false,
    health: null,
    ...overrides,
  }
}

const ZOOM = connection({
  id: 'zoom',
  kind: 'zoom',
  label: 'zoom',
  config: { account_id: 'acct-1', client_id: 'cid-1' },
  secrets_set: ['client_secret'],
})

interface Call {
  method: string
  path: string
  headers?: Record<string, string>
  body?: Record<string, unknown>
}

let calls: Call[]
let list: ConnectionView[]
let system: SystemInfo
type Responder = (call: Call) => ConnectionView | Error | undefined

/** A stand-in for the network: the write calls are answered by `respond`, the reads are fixed. */
function stubApi(respond: Responder = () => undefined) {
  calls = []
  vi.stubGlobal('$fetch', async (url: string, options: { method?: string; headers?: Record<string, string>; body?: string } = {}) => {
    const path = new URL(url).pathname
    const method = options.method ?? 'GET'
    const call: Call = { method, path, headers: options.headers, body: options.body ? JSON.parse(options.body) : undefined }
    calls.push(call)
    if (method === 'GET' && path === '/api/system') return system
    if (method === 'GET' && path === '/api/connections') return list
    if (method === 'GET' && path === '/api/config') return { schedule_cron: '0 6 * * *', tracker: 'none', active_sources: {} }
    const answer = respond(call)
    if (answer instanceof Error) throw answer
    if (method === 'DELETE') return undefined
    if (answer) return answer
    throw new Error(`unexpected ${method} ${path}`)
  })
}

function apiFailure(status: number, detail: unknown): Error {
  return Object.assign(new Error('fetch failed'), { status, data: { detail } })
}

function validation(...entries: Array<[string[], string]>): Error {
  return apiFailure(422, entries.map(([loc, msg]) => ({ type: 'value_error', loc, msg })))
}

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)
const input = (name: string) => $<HTMLInputElement>(`field-${name}`)
const fieldNames = () =>
  [...body.querySelectorAll('[data-testid^="field-"]')].map((el) => el.getAttribute('data-testid')?.slice('field-'.length))

async function type(name: string, value: string) {
  const el = input(name)
  if (!el) throw new Error(`no field ${name}`)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

async function submit() {
  const form = body.querySelector('form')
  if (!form) throw new Error('no form')
  form.dispatchEvent(new Event('submit', { cancelable: true }))
  await vi.waitFor(() => expect($('dialog-submit')?.classList.contains('q-btn--loading')).toBe(false))
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

async function mountDialog(props: { mode: 'create' | 'edit'; connection?: ConnectionView | null }) {
  const wrapper = await mountSuspended(ConnectionDialog, { props: { modelValue: true, ...props } })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

let notify: ReturnType<typeof vi.fn>

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

async function mountPage() {
  const wrapper = await mountSuspended(Harness, { attachTo: document.body })
  mounted.push(wrapper as VueWrapper<unknown>)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  list = []
  system = SYSTEM
  stubApi()
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
  vi.unstubAllGlobals()
})

describe('ConnectionDialog fields', () => {
  it('asks for exactly the API contract fields of each kind', async () => {
    await mountDialog({ mode: 'create' })
    const expected: Record<string, string[]> = {
      m365: ['alias', 'tenant_id', 'client_id'],
      // zoom defaults to oauth mode on create, which hides account_id.
      zoom: ['client_id', 'client_secret'],
      slack: ['label', 'token'],
      gmail: ['label', 'client_id', 'client_secret', 'redirect_mode'],
      // api_user is not asked for: the server defaults it to username (Namecheap only issues one
      // credential, the API key).
      namecheap: ['label', 'username', 'client_ip', 'api_key', 'sandbox', 'registrant_contact'],
      // godaddy defaults to pat mode on create, which hides api_key/api_secret.
      godaddy: ['label', 'api_token', 'environment', 'registrant_contact'],
      wordpress: ['label', 'client_id', 'client_secret', 'redirect_mode'],
    }
    let first = true
    for (const [kind, names] of Object.entries(expected)) {
      if (!first) await click('kind-change')
      first = false
      await click(`kind-${kind}`)
      expect(fieldNames()).toEqual(names)
    }
  })

  it('renders secret inputs as password fields with autocomplete off for password managers, with a reveal toggle', async () => {
    await mountDialog({ mode: 'create' })
    let first = true
    for (const [kind, secret] of [['zoom', 'client_secret'], ['slack', 'token'], ['gmail', 'client_secret']] as const) {
      if (!first) await click('kind-change')
      first = false
      await click(`kind-${kind}`)
      const el = input(secret)
      expect(el?.type).toBe('password')
      expect(el?.getAttribute('autocomplete')).toBe('new-password')
      expect(el?.value).toBe('')

      await click(`reveal-${secret}`)
      expect(input(secret)?.type).toBe('text')
      await click(`reveal-${secret}`)
      expect(input(secret)?.type).toBe('password')
    }
    await click('kind-change')
    await click('kind-m365')
    expect(body.querySelector('input[type="password"]')).toBeNull()
  })

  it('is persistent: a click outside does not dismiss it', async () => {
    const wrapper = await mountDialog({ mode: 'create' })
    body.querySelector<HTMLElement>('.q-dialog__backdrop')?.click()
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect($('connection-dialog')).not.toBeNull()
  })

  it('edit mode shows Saved and a Replace control, never a value; Replace reveals an empty password input', async () => {
    await mountDialog({ mode: 'edit', connection: ZOOM })

    expect($('dialog-title')?.textContent).toBe('Edit zoom')
    expect(fieldNames()).toEqual(['account_id', 'client_id'])
    expect(input('account_id')?.value).toBe('acct-1')
    expect($('saved-chip')?.textContent).toContain('Saved')
    expect(input('client_secret')).toBeNull()

    await click('replace-client_secret')
    const el = input('client_secret')
    expect(el?.type).toBe('password')
    expect(el?.getAttribute('autocomplete')).toBe('new-password')
    expect(el?.value).toBe('')
    expect($('saved-chip')).toBeNull()
  })

  it('edit mode hides the immutable label and notes that the UI store is authoritative for env connections', async () => {
    await mountDialog({ mode: 'edit', connection: connection({ origin: 'env' }) })
    expect(fieldNames()).toEqual([])
    expect($('env-note')?.textContent).toContain('authoritative')
  })
})

describe('ConnectionDialog zoom auth mode', () => {
  it('create defaults to oauth: hides account_id, shows the redirect block, and posts auth_mode/include_transcripts without account_id', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'zoom', kind: 'zoom' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-zoom')

    expect(fieldNames()).toEqual(['client_id', 'client_secret'])
    expect($('zoom-redirect-uri')?.textContent).toContain('/api/oauth/callback/zoom')
    expect($('field-account_id')).toBeNull()
    expect(body.textContent).not.toContain('refresh_token')
    expect($('field-refresh_token')).toBeNull()

    await type('client_id', 'cid-1')
    await type('client_secret', OTHER_SECRET)
    await submit()

    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({
      kind: 'zoom',
      auth_mode: 'oauth',
      client_id: 'cid-1',
      client_secret: OTHER_SECRET,
      include_transcripts: true,
    })
  })

  it('switching to s2s shows account_id, hides the redirect block, and posts account_id', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'zoom', kind: 'zoom' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-zoom')
    await click('zoom-auth-s2s')

    expect(fieldNames()).toEqual(['account_id', 'client_id', 'client_secret'])
    expect($('zoom-redirect-uri')).toBeNull()

    await type('account_id', 'acct-9')
    await type('client_id', 'cid-1')
    await type('client_secret', OTHER_SECRET)
    await submit()

    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({
      kind: 'zoom',
      auth_mode: 's2s',
      account_id: 'acct-9',
      client_id: 'cid-1',
      client_secret: OTHER_SECRET,
      include_transcripts: true,
    })
  })

  it('warns when no public base URL is configured', async () => {
    await mountDialog({ mode: 'create' })
    await click('kind-zoom')
    expect($('zoom-callback-missing')?.textContent).toContain('https PUBLIC_BASE_URL')
  })

  it('does not warn once a public base URL is configured', async () => {
    system = { ...SYSTEM, public_base_url_configured: true }
    await mountDialog({ mode: 'create' })
    await click('kind-zoom')
    expect($('zoom-callback-missing')).toBeNull()
  })

  it('offers a copy button for the zoom redirect path', async () => {
    await mountDialog({ mode: 'create' })
    await click('kind-zoom')
    expect($<HTMLInputElement>('zoom-redirect-path')?.value).toBe('/api/oauth/callback/zoom')
    expect($('copy-zoom-redirect')).not.toBeNull()
    await click('copy-zoom-redirect')
  })

  it('unchecking include transcripts posts include_transcripts: false', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'zoom', kind: 'zoom' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-zoom')
    await click('zoom-include-transcripts')
    await type('client_id', 'cid-1')
    await type('client_secret', OTHER_SECRET)
    await submit()

    expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({ include_transcripts: false })
  })

  it('edit initialises auth mode from config.auth_mode and the transcripts toggle from config.include_transcripts', async () => {
    const oauthConn = connection({
      id: 'zoom',
      kind: 'zoom',
      config: { client_id: 'cid-1', auth_mode: 'oauth', account_id: 'acct-legacy', include_transcripts: 'false' },
      secrets_set: ['client_secret'],
    })
    stubApi((call) => (call.method === 'PATCH' ? oauthConn : undefined))
    await mountDialog({ mode: 'edit', connection: oauthConn })

    // An explicit auth_mode of oauth wins even though account_id is set on the stored config.
    expect(fieldNames()).toEqual(['client_id'])
    expect($('zoom-redirect-uri')).not.toBeNull()

    await click('zoom-auth-s2s')
    expect(fieldNames()).toEqual(['account_id', 'client_id'])

    await type('account_id', 'acct-new')
    await submit()

    expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({
      config: { auth_mode: 's2s', account_id: 'acct-new' },
    })
  })

  it('edit defaults auth mode to s2s from a legacy config that has account_id but no auth_mode', async () => {
    await mountDialog({ mode: 'edit', connection: ZOOM })
    expect(fieldNames()).toEqual(['account_id', 'client_id'])
    expect($('zoom-redirect-uri')).toBeNull()
  })

  it('never renders refresh_token as an input for zoom', async () => {
    await mountDialog({ mode: 'create' })
    await click('kind-zoom')
    expect($('field-refresh_token')).toBeNull()
    await click('zoom-auth-s2s')
    expect($('field-refresh_token')).toBeNull()
  })
})

describe('ConnectionDialog submit', () => {
  it('create posts every field with the CSRF header and emits the saved connection', async () => {
    const saved = connection({ id: 'slack_acme' })
    stubApi((call) => (call.method === 'POST' ? saved : undefined))
    const wrapper = await mountDialog({ mode: 'create' })

    await click('kind-slack')
    await type('label', 'acme')
    await type('token', SECRET)
    await submit()

    const post = calls.find((call) => call.method === 'POST')
    expect(post?.path).toBe('/api/connections')
    expect(post?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(post?.body).toEqual({ kind: 'slack', label: 'acme', token: SECRET })
    expect(wrapper.emitted('saved')).toEqual([[saved, 'create']])
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
  })

  it('create sends the redirect mode for gmail', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'gmail_work', kind: 'gmail' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-gmail')
    await type('label', 'work')
    await type('client_id', 'cid.apps.example.com')
    await type('client_secret', OTHER_SECRET)
    await submit()

    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({
      kind: 'gmail',
      label: 'work',
      client_id: 'cid.apps.example.com',
      client_secret: OTHER_SECRET,
      redirect_mode: 'paste_back',
    })
  })

  it('create sends the registrar defaults and omits a blank registrant_contact, with no api_user field', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'namecheap_acme', kind: 'namecheap' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-namecheap')
    expect(body.textContent).toContain('this server’s public IPv4')
    expect($('field-api_user')).toBeNull()

    await type('label', 'acme')
    await type('username', 'acmeuser')
    await type('client_ip', '203.0.113.5')
    await type('api_key', SECRET)
    await submit()

    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({
      kind: 'namecheap',
      label: 'acme',
      username: 'acmeuser',
      client_ip: '203.0.113.5',
      api_key: SECRET,
      sandbox: 'false',
    })
  })

  it('detects the client IP and fills the field, or shows an error and leaves it alone', async () => {
    let egressIpResult: { ip: string } | Error = { ip: '198.51.100.7' }
    stubApi((call) => {
      if (call.method === 'GET' && call.path === '/api/domains/egress-ip') {
        return egressIpResult instanceof Error ? egressIpResult : (egressIpResult as unknown as ConnectionView)
      }
      return undefined
    })
    await mountDialog({ mode: 'create' })
    await click('kind-namecheap')

    await click('detect-client-ip')
    expect(($('field-client_ip') as HTMLInputElement).value).toBe('198.51.100.7')

    egressIpResult = Object.assign(new Error('fetch failed'), { status: 0 })
    await click('detect-client-ip')
    expect(body.textContent).toContain('Could not detect')
    // A failed detect must not clobber the value a previous successful detect (or manual entry) set.
    expect(($('field-client_ip') as HTMLInputElement).value).toBe('198.51.100.7')
  })

  it('create sends the registrant_contact JSON and the godaddy environment default (pat mode, the default)', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'godaddy_acme', kind: 'godaddy' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-godaddy')
    await type('label', 'acme')
    await type('api_token', SECRET)
    const contact = JSON.stringify({ first_name: 'Jamie' })
    await type('registrant_contact', contact)
    await submit()

    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({
      kind: 'godaddy',
      label: 'acme',
      api_token: SECRET,
      environment: 'production',
      auth_mode: 'pat',
      registrant_contact: contact,
    })
  })

  it('create sends api_key/api_secret in classic mode, never api_token', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'godaddy_acme', kind: 'godaddy' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-godaddy')
    await click('godaddy-auth-classic')
    await type('label', 'acme')
    await type('api_key', SECRET)
    await type('api_secret', OTHER_SECRET)
    await submit()

    const sentBody = calls.find((call) => call.method === 'POST')?.body as Record<string, unknown>
    expect(sentBody).toEqual({
      kind: 'godaddy',
      label: 'acme',
      api_key: SECRET,
      api_secret: OTHER_SECRET,
      environment: 'production',
      auth_mode: 'classic',
    })
    expect(sentBody.api_token).toBeUndefined()
  })

  it('validates registrant_contact as JSON and as an object, not an array, before it can be submitted', async () => {
    stubApi((call) => (call.method === 'POST' ? connection({ id: 'godaddy_acme', kind: 'godaddy' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-godaddy')
    await type('label', 'acme')
    await type('api_token', SECRET)

    await type('registrant_contact', 'not json')
    await submit()
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    expect(body.textContent).toContain('must be valid JSON')

    await type('registrant_contact', '["a","b"]')
    await submit()
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    expect(body.textContent).toContain('must be a JSON object')
  })

  it('does not ask for the wordpress access token: browser sign-in stores it', async () => {
    await mountDialog({ mode: 'create' })
    await click('kind-wordpress')
    expect(fieldNames()).toEqual(['label', 'client_id', 'client_secret', 'redirect_mode'])
  })

  it('validates on the client with the server rules and sends nothing', async () => {
    await mountDialog({ mode: 'create' })
    await click('kind-slack')
    await type('label', 'Not_Valid')
    await type('token', SECRET)
    await submit()

    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    expect(body.textContent).toContain('lowercase letters and digits')

    await click('kind-change')
    await click('kind-m365')
    await type('alias', 'has.dot')
    await type('tenant_id', 'tenant-1')
    await type('client_id', '')
    await submit()
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    expect(body.textContent).toContain('letters, digits and -')
    expect(body.textContent).toContain('Required')
  })

  it('edit sends only the config fields the user touched', async () => {
    stubApi((call) => (call.method === 'PATCH' ? ZOOM : undefined))
    await mountDialog({ mode: 'edit', connection: ZOOM })

    expect($<HTMLButtonElement>('dialog-submit')?.disabled).toBe(true)
    await type('client_id', 'cid-2')
    await submit()

    const patch = calls.find((call) => call.method === 'PATCH')
    expect(patch?.path).toBe('/api/connections/zoom')
    expect(patch?.headers?.['X-Requested-With']).toBe('signalslate')
    expect(patch?.body).toEqual({ config: { client_id: 'cid-2' } })
  })

  it('edit sends only the secret when only the secret was replaced', async () => {
    stubApi((call) => (call.method === 'PATCH' ? ZOOM : undefined))
    await mountDialog({ mode: 'edit', connection: ZOOM })

    await click('replace-client_secret')
    await type('client_secret', OTHER_SECRET)
    await submit()

    expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({ secrets: { client_secret: OTHER_SECRET } })
  })

  it('clears the secret after a successful submit', async () => {
    stubApi((call) => (call.method === 'POST' ? connection() : undefined))
    // No v-model handler: the dialog stays rendered so the field can be inspected after the save.
    await mountDialog({ mode: 'create' })
    await click('kind-slack')
    await type('label', 'acme')
    await type('token', SECRET)
    expect(input('token')?.value).toBe(SECRET)

    await submit()

    expect(calls.some((call) => call.method === 'POST')).toBe(true)
    expect(input('token')?.value).toBe('')
    expect(body.textContent).not.toContain(SECRET)
  })

  it('clears the secret after a failed submit, keeps the other fields, and never renders it', async () => {
    stubApi((call) => (call.method === 'POST' ? apiFailure(503, { code: 'secret_key_missing', message: 'No encryption key is configured; secrets cannot be stored' }) : undefined))
    await mountDialog({ mode: 'create' })
    await click('kind-slack')
    await type('label', 'acme')
    await type('token', SECRET)

    await submit()

    expect(input('token')?.value).toBe('')
    expect(input('label')?.value).toBe('acme')
    expect($('form-error')?.textContent).toContain('No encryption key is configured')
    expect(body.textContent).not.toContain(SECRET)
    expect(body.innerHTML).not.toContain(SECRET)
  })

  it('clears the secret when the dialog is closed', async () => {
    const wrapper = await mountDialog({ mode: 'create' })
    await click('kind-slack')
    await type('token', SECRET)
    await click('dialog-cancel')
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])

    await wrapper.setProps({ modelValue: false })
    await flushPromises()
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await click('kind-slack')
    expect(input('token')?.value).toBe('')
  })

  it('maps 422 field errors onto the fields by name and the rest onto the form', async () => {
    stubApi((call) =>
      call.method === 'POST'
        ? validation(
            [['body', 'slack', 'label'], 'must match [a-z0-9]+ and be at most 32 characters'],
            [['body', 'token'], 'must be at most 4096 characters with no control characters'],
            [['body'], 'unknown field'],
          )
        : undefined,
    )
    await mountDialog({ mode: 'create' })
    await click('kind-slack')
    await type('label', 'acme')
    await type('token', SECRET)
    await submit()

    const errorOf = (name: string) => input(name)?.closest('.q-field')?.querySelector('.q-field__messages')?.textContent
    expect(errorOf('label')).toBe('must match [a-z0-9]+ and be at most 32 characters')
    expect(errorOf('token')).toBe('must be at most 4096 characters with no control characters')
    expect($('form-error')?.textContent).toContain('unknown field')
    expect(input('token')?.value).toBe('')

    // Editing the field withdraws the server's message (the hint takes its place).
    await type('label', 'other')
    expect(errorOf('label')).not.toContain('must match')
  })

  it('shows a duplicate id on the label field', async () => {
    stubApi((call) =>
      call.method === 'POST' ? apiFailure(409, { code: 'duplicate_connection', message: 'A connection with this id already exists' }) : undefined,
    )
    await mountDialog({ mode: 'create' })
    await click('kind-slack')
    await type('label', 'acme')
    await type('token', SECRET)
    await submit()

    expect(input('label')?.closest('.q-field')?.querySelector('.q-field__messages')?.textContent).toBe(
      'A connection with this id already exists',
    )
  })
})

describe('connections page: add, edit, delete', () => {
  it('hides Add and Edit when no secret key is configured', async () => {
    system = { ...SYSTEM, secret_key_configured: false }
    list = [connection({ origin: 'env' })]
    await mountPage()
    expect($('conn-add')).toBeNull()
    // The overflow menu (Edit + Delete) is gated the same way: no secret store, no menu at all.
    expect($('conn-menu')).toBeNull()
    expect($('conn-edit')).toBeNull()
    expect($('conn-delete')).toBeNull()
  })

  it('Add creates the connection, says it starts inactive and never shows or notifies the secret', async () => {
    const created = connection()
    stubApi((call) => {
      if (call.method === 'POST') {
        list = [created]
        return created
      }
      return undefined
    })
    await mountPage()
    expect($('empty-state-title')?.textContent).toBe('No accounts yet')

    await click('conn-add')
    await click('kind-slack')
    await type('label', 'acme')
    await type('token', SECRET)
    expect(body.textContent).not.toContain(SECRET)
    await submit()

    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({ kind: 'slack', label: 'acme', token: SECRET })
    // The list was refreshed in place.
    expect(body.querySelectorAll('[data-testid="connection"]')).toHaveLength(1)
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ message: 'Created inactive: press Test, then enable' }))
    await vi.waitFor(() => expect(body.textContent).toContain('Created inactive: press Test, then enable'))
    expect(body.textContent).not.toContain(SECRET)
    expect(JSON.stringify(notify.mock.calls)).not.toContain(SECRET)
  })

  it('never lets the secret reach the DOM text or a notification after a failed save either', async () => {
    stubApi((call) => (call.method === 'POST' ? apiFailure(500, 'The store is unavailable') : undefined))
    await mountPage()
    await click('conn-add')
    await click('kind-slack')
    await type('label', 'acme')
    await type('token', SECRET)
    await submit()

    expect($('form-error')?.textContent).toContain('The store is unavailable')
    expect(body.textContent).not.toContain(SECRET)
    expect(JSON.stringify(notify.mock.calls)).not.toContain(SECRET)
  })

  it('Edit opens the dialog for that connection, saves and refreshes', async () => {
    list = [ZOOM]
    stubApi((call) => {
      if (call.method === 'PATCH') {
        list = [{ ...ZOOM, config: { ...ZOOM.config, client_id: 'cid-2' } }]
        return list[0]
      }
      return undefined
    })
    await mountPage()

    await click('conn-menu')
    await click('conn-edit')
    expect($('dialog-title')?.textContent).toBe('Edit zoom')
    await click('replace-client_secret')
    await type('client_secret', OTHER_SECRET)
    await type('client_id', 'cid-2')
    await submit()

    expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({
      config: { client_id: 'cid-2' },
      secrets: { client_secret: OTHER_SECRET },
    })
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ message: 'Saved zoom' }))
    expect(body.textContent).not.toContain(OTHER_SECRET)
    expect(JSON.stringify(notify.mock.calls)).not.toContain(OTHER_SECRET)
  })

  it('Delete asks first, naming the account and what is removed and kept, then deletes and refreshes', async () => {
    list = [connection()]
    stubApi((call) => {
      if (call.method === 'DELETE') {
        list = []
        return undefined
      }
      return undefined
    })
    await mountPage()

    await click('conn-menu')
    await click('conn-delete')
    const text = $('delete-confirm')?.textContent ?? ''
    // The card shows the label ("acme"), so the confirm names the same title, not the raw id.
    expect(text).toContain('Delete acme?')
    expect(text).toContain('credentials')
    expect(text).toContain('sync position')
    expect(text).toContain('on/off setting')
    expect(text).toContain('Items already collected are kept')
    expect(text).toContain('A .env entry will NOT bring it back')
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)

    await click('delete-confirm-btn')
    await vi.waitFor(() => expect(body.querySelectorAll('[data-testid="connection"]')).toHaveLength(0))

    const del = calls.find((call) => call.method === 'DELETE')
    expect(del?.path).toBe('/api/connections/slack_acme')
    expect(del?.headers?.['X-Requested-With']).toBe('signalslate')
    expect($('empty-state-title')?.textContent).toBe('No accounts yet')
  })

  it('Cancel on the delete confirm deletes nothing; a failed delete shows the error and keeps the card', async () => {
    list = [connection()]
    stubApi((call) => (call.method === 'DELETE' ? apiFailure(404, 'Connection not found') : undefined))
    await mountPage()

    await click('conn-menu')
    await click('conn-delete')
    await click('delete-cancel')
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)

    await click('conn-menu')
    await click('conn-delete')
    await click('delete-confirm-btn')
    await vi.waitFor(() => expect($('delete-error')?.textContent).toBe('Connection not found'))
    expect(body.querySelectorAll('[data-testid="connection"]')).toHaveLength(1)
  })
})
