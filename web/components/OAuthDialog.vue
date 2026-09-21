<template>
  <!-- persistent: a click outside or Esc must not drop a sign-in that is under way (the flow is single
       use and the pasted address holds the OAuth code). Closing is Cancel or Close. -->
  <q-dialog persistent :model-value="modelValue" @update:model-value="(value: boolean) => { if (!value) close() }">
    <q-card style="min-width: 360px; max-width: 560px; width: 100%" data-testid="oauth-dialog">
      <q-card-section>
        <div class="text-h6" data-testid="oauth-title">Sign in {{ connection?.id }}</div>
      </q-card-section>

      <q-card-section class="q-gutter-md">
        <q-banner v-if="provider === 'google'" dense class="bg-grey-3" data-testid="oauth-gmail-note">
          Set the Google consent screen to In production. While it is in Testing, Google expires the refresh
          token after 7 days.
        </q-banner>

        <q-banner v-if="stage === 'connected'" class="bg-positive text-white" data-testid="oauth-connected">
          Connected<template v-if="account"> as {{ account }}</template>
        </q-banner>

        <template v-else-if="stage === 'choose'">
          <q-btn-toggle
            v-model="mode"
            no-caps
            unelevated
            toggle-color="primary"
            :options="modeOptions"
            :disable="busy"
            data-testid="oauth-mode"
          />
          <div v-if="mode === 'paste_back'" class="text-body2" data-testid="oauth-mode-help">
            Works anywhere. You approve in a new tab, that tab ends on a page that fails to load, and you paste
            its address back here.
          </div>
          <div v-else class="text-body2" data-testid="oauth-mode-help">
            You approve in a new tab and the provider sends that tab back to this app. This page notices the
            sign-in by itself.
          </div>
        </template>

        <template v-else-if="mode === 'paste_back'">
          <div class="text-body2" data-testid="oauth-paste-help">
            Approve access in the tab that just opened. The browser will then end on a page that fails to load
            (the address starts with http://127.0.0.1 or http://localhost). That is expected. Copy the FULL
            address from the address bar and paste it below.
          </div>
          <q-banner v-if="provider === 'microsoft'" dense class="bg-warning text-black" data-testid="oauth-ms-warning">
            Microsoft sign-in codes live only about a minute. Paste the address straight away.
          </q-banner>
          <q-form autocomplete="off" @submit="onPaste">
            <q-input
              v-model="pasted"
              type="password"
              autocomplete="new-password"
              label="Address copied from the browser"
              outlined
              dense
              :disable="busy"
              data-testid="oauth-paste"
            />
            <div class="row items-center q-gutter-sm q-mt-sm">
              <q-btn
                type="submit"
                color="primary"
                no-caps
                label="Submit"
                :loading="busy"
                :disable="!pasted.trim()"
                data-testid="oauth-submit"
              />
            </div>
          </q-form>
        </template>

        <template v-else>
          <div class="row items-center q-gutter-sm" data-testid="oauth-callback-help">
            <q-spinner size="sm" color="primary" />
            <span>Waiting for you to approve access in the new tab. This page checks every 2 seconds.</span>
          </div>
        </template>

        <div v-if="stage === 'waiting'" class="row items-center q-gutter-sm">
          <div class="text-grey-8" data-testid="oauth-countdown">Expires in {{ countdown }}</div>
          <q-space />
          <q-btn flat dense no-caps label="Open the sign-in page again" data-testid="oauth-reopen" @click="reopen" />
        </div>

        <div v-if="pollWarning" class="text-grey-8" data-testid="oauth-poll-warning">{{ pollWarning }}</div>

        <q-banner v-if="errorText" dense class="bg-negative text-white" data-testid="oauth-error">
          {{ errorText }}
        </q-banner>
      </q-card-section>

      <q-card-actions align="right">
        <q-btn
          v-if="stage !== 'connected'"
          flat
          no-caps
          label="Cancel"
          :disable="busy"
          data-testid="oauth-cancel"
          @click="close"
        />
        <q-btn
          v-if="stage === 'choose'"
          color="primary"
          no-caps
          label="Start"
          :loading="busy"
          data-testid="oauth-start"
          @click="onStart"
        />
        <q-btn v-if="stage === 'connected'" color="primary" no-caps label="Close" data-testid="oauth-close" @click="close" />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { ApiError, parseUtc } from '~/composables/useApi'
import type { ConnectionView, OAuthMode, OAuthProvider, SystemInfo } from '~/composables/useApi'

const props = defineProps<{
  modelValue: boolean
  connection: ConnectionView | null
  system: SystemInfo | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  connected: [connection: ConnectionView]
}>()

const api = useApi()

const POLL_MS = 2000
const TICK_MS = 1000

// Every API failure code the start and paste routes can answer with (api/routers/oauth.py), mapped to
// fixed text: nothing the server sends is rendered.
const FALLBACK_MESSAGE = 'Sign-in failed. Start it again.'
const EXPIRED_MESSAGE = 'This sign-in expired. Press Start to begin again.'
const ERROR_MESSAGES: Record<string, string> = {
  invalid_pasted_url:
    'That is not the address the browser ended on. Copy the full address from the address bar and paste it again.',
  unknown_flow: 'This sign-in is no longer active. Press Start to begin again.',
  flow_expired: EXPIRED_MESSAGE,
  nonce_mismatch: 'This browser did not start that sign-in. Press Start to begin again in this browser.',
  provider_mismatch: 'That address belongs to a different provider. Press Start to begin again.',
  state_mismatch: 'That address belongs to a different sign-in. Press Start to begin again and use the newest tab.',
  consent_denied: 'Access was denied at the provider. Press Start and approve access.',
  scope_not_granted: 'The required permission was not granted. Press Start and approve every permission.',
  no_refresh_token: 'The provider did not return a refresh token. Press Start and approve offline access.',
  provider_error: 'The provider reported an error. Try again in a moment.',
  exchange_failed: 'The provider did not accept the sign-in. Press Start to begin again.',
  cache_write_failed: 'The sign-in was approved but its token could not be saved. Press Start to begin again.',
  callback_unavailable: 'Automatic callback needs an https PUBLIC_BASE_URL. Use paste-back instead.',
  invalid_mode: 'That sign-in mode is not supported.',
  connection_not_found: 'This connection no longer exists.',
  client_credentials_missing: 'Save the client ID and client secret on this connection first.',
  invalid_alias: 'This connection alias cannot be used to sign in.',
  provider_kind_mismatch: 'This connection cannot sign in with that provider.',
  unknown_provider: 'Unknown sign-in provider.',
  secret_key_missing: 'Set SIGNALSLATE_SECRET_KEY in .env so secrets can be stored.',
  secret_decrypt_failed: 'Stored secrets cannot be decrypted with the installed key.',
  connection_error: 'The connection could not be updated.',
  sign_in_failed: FALLBACK_MESSAGE,
}
// The flow survives these two (the server rejects them before it consumes anything), so the paste box stays.
const RETRYABLE = new Set(['invalid_pasted_url', 'nonce_mismatch'])

type Stage = 'choose' | 'waiting' | 'connected'

const provider = computed<OAuthProvider>(() => (props.connection?.kind === 'm365' ? 'microsoft' : 'google'))
const availableModes = computed<OAuthMode[]>(() => props.system?.oauth?.[provider.value]?.modes ?? [])
const modeOptions = computed(() => {
  const options = [{ label: 'Paste-back', value: 'paste_back', attrs: { 'data-testid': 'mode-paste_back' } }]
  if (availableModes.value.includes('callback')) {
    options.push({ label: 'Automatic callback', value: 'callback', attrs: { 'data-testid': 'mode-callback' } })
  }
  return options
})

const stage = ref<Stage>('choose')
const mode = ref<OAuthMode>('paste_back')
const busy = ref(false)
const errorText = ref<string | null>(null)
const pollWarning = ref<string | null>(null)
const account = ref<string | null>(null)
// The pasted address holds the OAuth code: it lives only here, and is cleared in the finally of the
// request, on every close and on unmount. It never reaches a toast, a route or a log line.
const pasted = ref('')
const now = ref(Date.now())
const expiresAt = ref(0)

let flowId: string | null = null
let authUrl: string | null = null
let baseline = { signedIn: false, checkedAt: null as string | null }
// Bumped whenever a sign-in starts over or the dialog closes, so a late response from the old one is dropped.
let session = 0
let polling = false
let tickTimer: ReturnType<typeof setInterval> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null

const countdown = computed(() => {
  const seconds = Math.max(0, Math.ceil((expiresAt.value - now.value) / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
})

function stopTimers() {
  if (tickTimer !== null) clearInterval(tickTimer)
  if (pollTimer !== null) clearInterval(pollTimer)
  tickTimer = null
  pollTimer = null
}

function reset() {
  session += 1
  stopTimers()
  stage.value = 'choose'
  mode.value = 'paste_back'
  busy.value = false
  errorText.value = null
  pollWarning.value = null
  account.value = null
  pasted.value = ''
  flowId = null
  authUrl = null
  polling = false
}

function messageFor(error: unknown): { text: string; code: string | undefined } {
  if (error instanceof ApiError) {
    if (error.status === 0) return { text: 'The API could not be reached.', code: undefined }
    return { text: (error.code && ERROR_MESSAGES[error.code]) || FALLBACK_MESSAGE, code: error.code }
  }
  return { text: FALLBACK_MESSAGE, code: undefined }
}

/** Opens the provider's page. Only https is opened: the URL came from the API, but it is opened in a tab, so it is checked. */
function openAuthUrl(url: string): boolean {
  if (!/^https:\/\//i.test(url)) return false
  window.open(url, '_blank', 'noopener,noreferrer')
  return true
}

function reopen() {
  if (authUrl) openAuthUrl(authUrl)
}

function expire() {
  session += 1
  stopTimers()
  stage.value = 'choose'
  flowId = null
  authUrl = null
  pasted.value = ''
  pollWarning.value = null
  errorText.value = EXPIRED_MESSAGE
}

function tick() {
  now.value = Date.now()
  if (stage.value === 'waiting' && now.value >= expiresAt.value) expire()
}

function isSignedIn(conn: ConnectionView): boolean {
  const ready = provider.value === 'google' ? conn.secrets_set.includes('refresh_token') : conn.health?.status === 'ok'
  if (!ready) return false
  // A connection that was already signed in looks signed in before the flow ends: only a fresh health
  // check tells the two apart.
  return !baseline.signedIn || (conn.health?.checked_at ?? null) !== baseline.checkedAt
}

function finish(view: ConnectionView, name: string | null) {
  session += 1
  stopTimers()
  stage.value = 'connected'
  account.value = name
  errorText.value = null
  pollWarning.value = null
  flowId = null
  authUrl = null
  emit('connected', view)
}

async function poll(owner: number) {
  if (polling) return
  polling = true
  try {
    const list = await api.listConnections()
    if (owner !== session || stage.value !== 'waiting') return
    pollWarning.value = null
    const conn = list.find((entry) => entry.id === props.connection?.id)
    if (conn && isSignedIn(conn)) finish(conn, null)
  } catch {
    if (owner === session) pollWarning.value = 'Could not check the connection just now. Trying again.'
  } finally {
    polling = false
  }
}

async function onStart() {
  const conn = props.connection
  if (!conn || busy.value) return
  busy.value = true
  errorText.value = null
  const owner = ++session
  try {
    const started = await api.oauthStart(provider.value, { connection_id: conn.id, mode: mode.value })
    if (owner !== session) return
    const expiry = parseUtc(started.expires_at).getTime()
    if (Number.isNaN(expiry) || !openAuthUrl(started.auth_url)) {
      errorText.value = 'The API returned an unexpected sign-in address, so nothing was opened. Press Start to try again.'
      return
    }
    flowId = started.flow_id
    authUrl = started.auth_url
    expiresAt.value = expiry
    now.value = Date.now()
    baseline = {
      signedIn: provider.value === 'google' ? conn.secrets_set.includes('refresh_token') : conn.health?.status === 'ok',
      checkedAt: conn.health?.checked_at ?? null,
    }
    stage.value = 'waiting'
    stopTimers()
    tickTimer = setInterval(tick, TICK_MS)
    if (started.mode === 'callback') pollTimer = setInterval(() => void poll(owner), POLL_MS)
  } catch (error) {
    if (owner === session) errorText.value = messageFor(error).text
  } finally {
    if (owner === session) busy.value = false
  }
}

async function onPaste() {
  const conn = props.connection
  const id = flowId
  if (!conn || !id || busy.value || !pasted.value.trim()) return
  busy.value = true
  errorText.value = null
  const owner = session
  try {
    const result = await api.oauthPaste(provider.value, { flow_id: id, url: pasted.value.trim() })
    if (owner === session) finish(result.connection, result.account)
    else emit('connected', result.connection)
  } catch (error) {
    if (owner !== session) return
    const { text, code } = messageFor(error)
    errorText.value = text
    if (!code || !RETRYABLE.has(code)) {
      // The flow is spent (or unreachable), so the paste box would only ever fail again.
      session += 1
      stopTimers()
      stage.value = 'choose'
      flowId = null
      authUrl = null
    }
  } finally {
    pasted.value = ''
    busy.value = false
  }
}

function close() {
  reset()
  emit('update:modelValue', false)
}

watch(() => props.modelValue, reset, { immediate: true })

onBeforeUnmount(() => {
  session += 1
  stopTimers()
  pasted.value = ''
})
</script>
