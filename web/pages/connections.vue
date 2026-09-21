<template>
  <q-page padding class="q-gutter-md" style="max-width: 900px">
    <div class="text-h5">Connections</div>

    <q-banner v-if="system && !system.secret_key_configured" class="bg-warning text-black" data-testid="key-banner">
      Set SIGNALSLATE_SECRET_KEY in .env to manage connections here; env-only mode keeps working
    </q-banner>

    <div v-if="loading" class="row justify-center q-pa-lg" data-testid="loading">
      <q-spinner size="lg" color="primary" />
    </div>

    <q-banner v-else-if="loadError" class="bg-negative text-white" data-testid="load-error">
      {{ loadError }}
      <template #action>
        <q-btn flat label="Retry" @click="load" />
      </template>
    </q-banner>

    <div v-else-if="!connections.length" class="text-grey" data-testid="empty">No connections yet</div>

    <template v-else>
      <q-card v-for="conn in connections" :key="conn.id" data-testid="connection">
        <q-card-section>
          <div class="row items-center q-gutter-sm">
            <div class="text-subtitle1" data-testid="conn-id">{{ conn.id }}</div>
            <q-badge color="primary" outline data-testid="conn-kind">{{ conn.kind }}</q-badge>
            <q-badge :color="conn.origin === 'ui' ? 'secondary' : 'grey-7'" data-testid="conn-origin">
              {{ conn.origin }}
            </q-badge>
            <q-space />
            <q-toggle
              :model-value="conn.active"
              :disable="toggling"
              label="Active"
              data-testid="conn-active"
              @update:model-value="(value: boolean) => onToggle(conn, value)"
            />
          </div>

          <div class="row items-center q-gutter-sm q-mt-sm" data-testid="conn-health">
            <q-badge :color="healthColor(conn)" rounded data-testid="health-dot" />
            <span>{{ healthLabel(conn) }}</span>
            <span v-if="conn.health?.detail" class="text-grey-8">{{ conn.health.detail }}</span>
            <span v-if="conn.health?.checked_at" class="text-grey">{{ relativeTime(conn.health.checked_at) }}</span>
          </div>

          <div v-if="conn.secrets_set.length" class="q-mt-sm">
            <q-chip
              v-for="name in conn.secrets_set"
              :key="name"
              dense
              data-testid="secret-chip"
            >
              {{ name }}
            </q-chip>
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

        <q-card-actions>
          <q-btn
            flat
            color="primary"
            icon="network_check"
            label="Test"
            :loading="testing[conn.id] === true"
            data-testid="conn-test"
            @click="onTest(conn)"
          />
        </q-card-actions>
      </q-card>
    </template>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { ApiError, parseUtc } from '~/composables/useApi'
import type { ConnectionCheck, ConnectionView, SystemInfo } from '~/composables/useApi'

const api = useApi()
const $q = useQuasar()

const system = ref<SystemInfo | null>(null)
const connections = ref<ConnectionView[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const testing = reactive<Record<string, boolean>>({})
const testResults = reactive<Record<string, ConnectionCheck | undefined>>({})
const toggleErrors = reactive<Record<string, string | undefined>>({})
// One flag for every toggle: active_sources is a single document that each toggle reads, edits and
// writes back, so two in flight at once would let the later write drop the earlier change.
const toggling = ref(false)

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong'
}

async function load() {
  loading.value = true
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

function healthColor(conn: ConnectionView): string {
  if (!conn.health) return 'grey'
  return { ok: 'positive', error: 'negative' }[conn.health.status] ?? 'grey'
}

function healthLabel(conn: ConnectionView): string {
  return conn.health ? conn.health.status : 'not checked yet'
}

function relativeTime(iso: string): string {
  const then = parseUtc(iso).getTime()
  if (Number.isNaN(then)) return ''
  const seconds = Math.round((then - Date.now()) / 1000)
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ['day', 86400],
    ['hour', 3600],
    ['minute', 60],
  ]
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit)
  }
  return formatter.format(seconds, 'second')
}

async function onToggle(conn: ConnectionView, value: boolean) {
  toggling.value = true
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
    $q.notify({ type: 'negative', message: `Could not change ${conn.id}: ${message}` })
  } finally {
    toggling.value = false
  }
}

async function onTest(conn: ConnectionView) {
  testing[conn.id] = true
  try {
    const result = await api.testConnection(conn.id)
    testResults[conn.id] = result
    $q.notify({
      type: result.status === 'ok' ? 'positive' : 'negative',
      message: `${conn.id}: ${result.detail}`,
    })
  } catch (error) {
    const message = errorText(error)
    testResults[conn.id] = { status: 'error', detail: message }
    $q.notify({ type: 'negative', message: `${conn.id}: ${message}` })
  } finally {
    testing[conn.id] = false
  }
}

onMounted(load)
</script>
