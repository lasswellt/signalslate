<template>
  <q-page class="page-container q-pa-md">
    <PageHeader title="Accounts" subtitle="Services SignalSlate reads from">
      <template #actions>
        <q-btn
          v-if="system?.secret_key_configured"
          color="primary"
          icon="add"
          label="Add account"
          no-caps
          data-testid="conn-add"
          @click="openAdd"
        />
      </template>
    </PageHeader>

    <q-banner v-if="system && !system.secret_key_configured" rounded class="bg-warning text-black q-my-md" data-testid="key-banner">
      You can add and edit accounts here once a one-time setup step is done. Accounts already set through your
      server's environment keep working either way.
      <q-expansion-item dense label="How to enable" header-class="text-black" class="q-mt-sm" data-testid="key-banner-howto">
        <div class="q-pa-sm text-black">
          Add <code>SIGNALSLATE_SECRET_KEY</code> to your <code>.env</code> file, then restart SignalSlate.
        </div>
      </q-expansion-item>
    </q-banner>

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="connections.length === 0"
      empty-icon="link"
      empty-title="No accounts yet"
      empty-message="Add an account to start collecting from it."
      skeleton="cards"
      @retry="load()"
    >
      <div v-for="group in groups" :key="group.kind" class="q-mt-md q-mb-lg" data-testid="kind-group">
        <div class="text-subtitle1 text-weight-medium q-mb-sm" data-testid="kind-group-title">{{ group.label }}</div>

        <q-card v-for="conn in group.connections" :key="conn.id" flat bordered class="q-mb-sm" data-testid="connection">
          <q-card-section>
            <div class="row items-start no-wrap q-gutter-sm">
              <div class="col-grow" style="min-width: 0">
                <div class="text-subtitle1 ellipsis" data-testid="conn-label">{{ conn.label || conn.id }}</div>
                <div class="text-caption text-grey" data-testid="conn-id">{{ conn.id }}</div>
              </div>
              <div class="text-caption text-grey" data-testid="conn-origin">
                {{ conn.origin === 'ui' ? 'Added here' : 'Set in .env' }}
              </div>
            </div>

            <div class="row items-center q-gutter-sm q-mt-sm" data-testid="conn-health">
              <StatusChip
                kind="health"
                :value="conn.health?.status ?? null"
                :label-override="conn.health ? undefined : 'Not checked yet'"
                dense
              />
              <span v-if="conn.health?.detail" class="text-grey-8">{{ conn.health.detail }}</span>
              <span v-if="conn.health?.checked_at" class="text-grey">{{ relativeTime(conn.health.checked_at) }}</span>
            </div>

            <div v-if="conn.secrets_set.length" class="text-caption text-grey-8 q-mt-sm" data-testid="conn-secrets">
              Credentials saved: {{ conn.secrets_set.map((name) => humanize(name)).join(', ') }}
            </div>

            <div v-if="toggleErrors[conn.id]" class="q-mt-sm text-negative" data-testid="toggle-error">
              {{ toggleErrors[conn.id] }}
            </div>
            <div
              v-if="testResults[conn.id]"
              class="q-mt-sm"
              :class="testResults[conn.id]?.status === 'ok' ? 'text-positive' : 'text-negative'"
              data-testid="test-result"
            >
              {{ testResults[conn.id]?.status }}: {{ testResults[conn.id]?.detail }}
            </div>
          </q-card-section>

          <q-card-actions class="row items-center q-gutter-sm">
            <q-btn
              flat
              no-caps
              color="primary"
              icon="network_check"
              label="Test"
              :loading="testing[conn.id] === true"
              data-testid="conn-test"
              @click="onTest(conn)"
            />
            <q-btn
              v-if="showSignIn(conn)"
              flat
              no-caps
              color="primary"
              icon="login"
              label="Sign in"
              data-testid="conn-signin"
              @click="openSignIn(conn)"
            />
            <q-space />
            <q-toggle
              :model-value="conn.active"
              :disable="togglingIds.has(conn.id)"
              label="Active"
              data-testid="conn-active"
              @update:model-value="(value: boolean) => onToggle(conn, value)"
            />
            <q-btn
              v-if="system?.secret_key_configured"
              flat
              round
              dense
              icon="more_vert"
              :aria-label="`More actions for ${accountTitle(conn)}`"
              data-testid="conn-menu"
            >
              <q-tooltip>{{ `More actions for ${accountTitle(conn)}` }}</q-tooltip>
              <q-menu>
                <q-list style="min-width: 160px">
                  <q-item clickable v-close-popup data-testid="conn-edit" @click="openEdit(conn)">
                    <q-item-section avatar><q-icon name="edit" /></q-item-section>
                    <q-item-section>Edit</q-item-section>
                  </q-item>
                  <q-item clickable v-close-popup data-testid="conn-delete" @click="askDelete(conn)">
                    <q-item-section avatar><q-icon name="delete" color="negative" /></q-item-section>
                    <q-item-section class="text-negative">Delete</q-item-section>
                  </q-item>
                </q-list>
              </q-menu>
            </q-btn>
          </q-card-actions>
        </q-card>
      </div>
    </AsyncState>

    <ConnectionDialog v-model="dialogOpen" :mode="dialogMode" :connection="dialogConnection" :system="system" @saved="onSaved" />

    <OAuthDialog v-model="signInOpen" :connection="signInConnection" :system="system" @connected="onSignedIn" />

    <q-dialog :model-value="deleteTarget !== null" @update:model-value="(open: boolean) => { if (!open) deleteTarget = null }">
      <q-card style="max-width: 480px" data-testid="delete-confirm">
        <q-card-section>
          <div class="text-h6">Delete {{ deleteTarget ? accountTitle(deleteTarget) : '' }}?</div>
        </q-card-section>
        <q-card-section class="q-pt-none">
          <p>
            SignalSlate will stop collecting from {{ deleteTarget ? accountTitle(deleteTarget) : '' }} and forget its
            saved credentials, sync position and on/off setting. Items already collected are kept.
          </p>
          <p class="q-mb-none">
            A .env entry will NOT bring it back: deleting it here keeps this account deleted until you add it again.
          </p>
          <div v-if="deleteError" class="q-mt-sm text-negative" data-testid="delete-error">{{ deleteError }}</div>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" :disable="deleting" data-testid="delete-cancel" @click="deleteTarget = null" />
          <q-btn
            color="negative"
            no-caps
            label="Delete"
            :loading="deleting"
            data-testid="delete-confirm-btn"
            @click="confirmDelete"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import PageHeader from '~/components/ui/PageHeader.vue'
import AsyncState from '~/components/ui/AsyncState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import { PROVIDER_FOR_KIND, zoomAuthMode } from '~/composables/useApi'
import type { ConnectionCheck, ConnectionKind, ConnectionView, SystemInfo } from '~/composables/useApi'

const api = useApi()
const $q = useQuasar()

const system = ref<SystemInfo | null>(null)
const connections = ref<ConnectionView[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const testing = reactive<Record<string, boolean>>({})
const testResults = reactive<Record<string, ConnectionCheck | undefined>>({})
const toggleErrors = reactive<Record<string, string | undefined>>({})
// Each toggle saves independently: a Set of in-flight ids disables only the card being saved.
const togglingIds = reactive(new Set<string>())

/** Display name for a connection: its label, falling back to the id when the label is blank. */
function accountTitle(conn: ConnectionView): string {
  return conn.label || conn.id
}

const KIND_LABELS: Record<ConnectionKind, string> = {
  m365: 'Microsoft 365',
  zoom: 'Zoom',
  slack: 'Slack',
  gmail: 'Gmail',
  namecheap: 'Namecheap',
  godaddy: 'GoDaddy',
  wordpress: 'WordPress',
}

interface ConnectionGroup {
  kind: ConnectionKind
  label: string
  connections: ConnectionView[]
}

// Grouped by kind, in the fixed order the kinds are declared (KIND_LABELS), so section order never
// jumps around as connections are added or removed.
const groups = computed<ConnectionGroup[]>(() => {
  const byKind = new Map<ConnectionKind, ConnectionView[]>()
  for (const conn of connections.value) {
    const list = byKind.get(conn.kind)
    if (list) list.push(conn)
    else byKind.set(conn.kind, [conn])
  }
  return (Object.keys(KIND_LABELS) as ConnectionKind[])
    .filter((kind) => byKind.has(kind))
    .map((kind) => ({ kind, label: KIND_LABELS[kind], connections: byKind.get(kind) as ConnectionView[] }))
})

// `silent` refreshes in place: after a save or delete the list must not collapse into the spinner.
async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  try {
    const [info, list] = await Promise.all([api.getSystem(), api.listConnections()])
    system.value = info
    connections.value = list
  } catch (error) {
    loadError.value = errorText(error)
  } finally {
    loading.value = false
  }
}

async function onToggle(conn: ConnectionView, value: boolean) {
  togglingIds.add(conn.id)
  toggleErrors[conn.id] = undefined
  const previous = conn.active
  conn.active = value
  try {
    const config = await api.getConfig()
    const saved = await api.updateConfig({
      ...config,
      active_sources: { ...config.active_sources, [conn.id]: value },
    })
    conn.active = saved.active_sources[conn.id] ?? value
  } catch (error) {
    conn.active = previous
    const message = errorText(error)
    toggleErrors[conn.id] = message
    $q.notify({ type: 'negative', message: `Could not change ${accountTitle(conn)}: ${message}` })
  } finally {
    togglingIds.delete(conn.id)
  }
}

async function onTest(conn: ConnectionView) {
  testing[conn.id] = true
  try {
    testResults[conn.id] = await api.testConnection(conn.id)
  } catch (error) {
    // The request itself failed (network/API error), not just an app-level "not ok" result: this
    // is the one case that still gets a toast, since the inline result never got set.
    const message = errorText(error)
    testResults[conn.id] = { status: 'error', detail: message }
    $q.notify({ type: 'negative', message: `Could not test ${accountTitle(conn)}: ${message}` })
  } finally {
    testing[conn.id] = false
  }
}

const dialogOpen = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const dialogConnection = ref<ConnectionView | null>(null)

function openAdd() {
  dialogMode.value = 'create'
  dialogConnection.value = null
  dialogOpen.value = true
}

function openEdit(conn: ConnectionView) {
  dialogMode.value = 'edit'
  dialogConnection.value = conn
  dialogOpen.value = true
}

async function onSaved(saved: ConnectionView, mode: 'create' | 'edit') {
  // Only the id goes into the toast: the dialog never hands over what was typed.
  $q.notify(
    mode === 'create'
      ? { type: 'positive', message: 'Created inactive: press Test, then enable', caption: saved.id }
      : { type: 'positive', message: `Saved ${saved.id}` },
  )
  await load(true)
}

// gmail and m365 always sign in through OAuth; zoom only when its effective auth_mode is "oauth"
// (a Server-to-Server zoom connection has no browser sign-in step).
function showSignIn(conn: ConnectionView): boolean {
  if (!system.value?.secret_key_configured) return false
  const provider = PROVIDER_FOR_KIND[conn.kind]
  if (!provider) return false
  return provider !== 'zoom' || zoomAuthMode(conn) === 'oauth'
}

const signInOpen = ref(false)
const signInConnection = ref<ConnectionView | null>(null)

function openSignIn(conn: ConnectionView) {
  signInConnection.value = conn
  signInOpen.value = true
}

async function onSignedIn(signed: ConnectionView) {
  $q.notify({ type: 'positive', message: `Signed in ${signed.id}` })
  await load(true)
}

// What ?oauth=error&reason= can carry (api/routers/oauth.py _REASONS). The query is untrusted text, so
// only a known code maps to a message and nothing from it is ever shown.
const LANDING_MESSAGES: Record<string, string> = {
  invalid_request: 'The sign-in request was not recognised. Start the sign-in again from this page.',
  expired: 'The sign-in took too long and expired. Start it again.',
  denied: 'Access was denied at the provider. Start again and approve access.',
  provider_error: 'The provider reported an error. Try again in a moment.',
  exchange_failed: 'The provider did not accept the sign-in. Start it again.',
  scope_missing: 'The required permission was not granted. Start again and approve every permission.',
  no_refresh_token: 'The provider did not return a refresh token. Start again and approve offline access.',
  failed: 'Sign-in failed. Start it again.',
}

const route = useRoute()
const router = useRouter()

function handleLanding() {
  const outcome = route.query.oauth
  if (outcome !== 'ok' && outcome !== 'error') return
  if (outcome === 'ok') {
    $q.notify({ type: 'positive', message: 'Sign-in complete' })
  } else {
    const reason = route.query.reason
    const message = (typeof reason === 'string' && Object.hasOwn(LANDING_MESSAGES, reason) && LANDING_MESSAGES[reason]) || LANDING_MESSAGES.failed
    $q.notify({ type: 'negative', message })
  }
  void router.replace({ path: route.path, query: {} })
}

const deleteTarget = ref<ConnectionView | null>(null)
const deleting = ref(false)
const deleteError = ref<string | null>(null)

function askDelete(conn: ConnectionView) {
  deleteError.value = null
  deleteTarget.value = conn
}

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target) return
  deleting.value = true
  deleteError.value = null
  try {
    await api.deleteConnection(target.id)
    deleteTarget.value = null
    $q.notify({ type: 'positive', message: `Deleted ${accountTitle(target)}` })
    await load(true)
  } catch (error) {
    deleteError.value = errorText(error)
    $q.notify({ type: 'negative', message: `Could not delete ${accountTitle(target)}: ${deleteError.value}` })
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  handleLanding()
  return load()
})
</script>
