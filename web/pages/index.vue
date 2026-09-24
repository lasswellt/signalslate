<template>
  <q-page class="page-container q-pa-md">
    <PageHeader title="Overview">
      <template #actions>
        <q-btn
          color="primary"
          icon="play_arrow"
          no-caps
          :label="triggering ? 'Running…' : 'Run digest now'"
          :loading="triggering"
          data-testid="trigger-run"
          @click="onTrigger"
        />
      </template>
    </PageHeader>

    <AsyncState :loading="loading" :error="loadError" skeleton="cards" @retry="load">
      <div class="row q-col-gutter-md q-mb-md" data-testid="kpi-row">
        <div class="col-12 col-sm-6 col-md-3">
          <q-card class="kpi-card" data-testid="kpi-last-run">
            <q-card-section>
              <div class="text-caption text-grey-7">Last run</div>
              <div v-if="!status?.last_run" class="kpi-value text-grey" data-testid="kpi-last-run-empty">No runs yet</div>
              <NuxtLink v-else class="kpi-link kpi-value" :to="`/history/${status.last_run.id}`" data-testid="kpi-last-run-link">
                <StatusChip kind="run" :value="status.last_run.status" />
                <div class="text-caption text-grey-7 q-mt-xs">{{ relativeTime(status.last_run.started_at) }}</div>
              </NuxtLink>
            </q-card-section>
          </q-card>
        </div>

        <div class="col-12 col-sm-6 col-md-3">
          <q-card class="kpi-card" data-testid="kpi-next-run">
            <q-card-section>
              <div class="text-caption text-grey-7">Next run</div>
              <div v-if="!status?.next_scheduled_run" class="kpi-value text-grey" data-testid="kpi-next-run-empty">
                Not scheduled
              </div>
              <div v-else class="kpi-value" data-testid="kpi-next-run-value">
                <div>{{ formatDate(status.next_scheduled_run) }}</div>
                <div class="text-caption text-grey-7">{{ relativeTime(status.next_scheduled_run) }}</div>
              </div>
            </q-card-section>
          </q-card>
        </div>

        <div class="col-12 col-sm-6 col-md-3">
          <q-card class="kpi-card" data-testid="kpi-accounts">
            <q-card-section>
              <div class="text-caption text-grey-7">Accounts</div>
              <div v-if="connectionsUnavailable" class="kpi-value text-grey" data-testid="kpi-accounts-unavailable">
                Unavailable
              </div>
              <div v-else class="kpi-value text-h6" data-testid="kpi-accounts-value">
                {{ accountsHealthy }} healthy / {{ accountsTotal }} total
              </div>
            </q-card-section>
          </q-card>
        </div>

        <div class="col-12 col-sm-6 col-md-3">
          <q-card class="kpi-card" data-testid="kpi-collectors">
            <q-card-section>
              <div class="text-caption text-grey-7">Collectors</div>
              <div v-if="collectorsUnavailable" class="kpi-value text-grey" data-testid="kpi-collectors-unavailable">
                Unavailable
              </div>
              <div v-else class="kpi-value text-h6" data-testid="kpi-collectors-value">
                {{ collectorsFailing }} failing
              </div>
            </q-card-section>
          </q-card>
        </div>
      </div>

      <q-card data-testid="health-card">
        <q-card-section>
          <div class="text-subtitle1 q-mb-sm">Source health</div>
          <HealthList :items="status?.source_health ?? []" />
        </q-card-section>
      </q-card>
    </AsyncState>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import PageHeader from '~/components/ui/PageHeader.vue'
import AsyncState from '~/components/ui/AsyncState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import HealthList from '~/components/HealthList.vue'
import type { CollectorState, ConnectionView, StatusResponse } from '~/composables/useApi'

// Poll cadence and cap for the "run finished" wait after Run digest now: fast enough to feel live,
// capped so a stuck run doesn't poll forever.
const POLL_INTERVAL_MS = 3000
const POLL_CAP_MS = 5 * 60_000

const api = useApi()
const $q = useQuasar()

const status = ref<StatusResponse | null>(null)
const connections = ref<ConnectionView[] | null>(null)
const collectors = ref<CollectorState[] | null>(null)
const connectionsUnavailable = ref(false)
const collectorsUnavailable = ref(false)

const loading = ref(true)
const loadError = ref<string | null>(null)
const triggering = ref(false)

let pollTimer: ReturnType<typeof setInterval> | null = null
let pollElapsedMs = 0

const accountsHealthy = computed(() => connections.value?.filter((c) => c.health?.status === 'ok').length ?? 0)
const accountsTotal = computed(() => connections.value?.length ?? 0)
const collectorsFailing = computed(() => collectors.value?.filter((c) => c.consecutive_failures > 0).length ?? 0)

/**
 * Loads /status, /connections and /collectors in parallel. /status failing is a page-level error
 * (nothing meaningful renders without it); a failing connections/collectors fetch only degrades its
 * own KPI tile to "Unavailable" so the rest of the page still works.
 */
async function load() {
  loading.value = true
  loadError.value = null
  connectionsUnavailable.value = false
  collectorsUnavailable.value = false

  const [statusResult, connectionsResult, collectorsResult] = await Promise.allSettled([
    api.getStatus(),
    api.listConnections(),
    api.listCollectors(),
  ])

  if (statusResult.status === 'fulfilled') {
    status.value = statusResult.value
  } else {
    status.value = null
    loadError.value = errorText(statusResult.reason)
  }

  if (connectionsResult.status === 'fulfilled') {
    connections.value = connectionsResult.value
  } else {
    connections.value = null
    connectionsUnavailable.value = true
  }

  if (collectorsResult.status === 'fulfilled') {
    collectors.value = collectorsResult.value
  } else {
    collectors.value = null
    collectorsUnavailable.value = true
  }

  loading.value = false
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  triggering.value = false
}

/**
 * Triggers a run and polls /status until a run other than the one that was last known finishes (or
 * the cap elapses). Recording the pre-trigger id first means a fast health-check run that finishes
 * before the first poll tick still counts as "new", instead of the old status !== 'running' check
 * that could match the run already sitting there before the click.
 */
async function onTrigger() {
  const previousId = status.value?.last_run?.id ?? null
  triggering.value = true
  try {
    await api.triggerRun()
  } catch (error) {
    triggering.value = false
    $q.notify({ type: 'negative', message: errorText(error) })
    return
  }

  pollElapsedMs = 0
  pollTimer = setInterval(async () => {
    pollElapsedMs += POLL_INTERVAL_MS
    try {
      status.value = await api.getStatus()
    } catch {
      // A transient poll failure shouldn't stop the run or the spinner; the next tick retries.
    }
    const run = status.value?.last_run
    if (run && run.id !== previousId && run.status !== 'running') {
      stopPolling()
      return
    }
    if (pollElapsedMs >= POLL_CAP_MS) {
      stopPolling()
      $q.notify({ type: 'negative', message: 'The run is taking longer than expected. Check History for its status.' })
    }
  }, POLL_INTERVAL_MS)
}

onMounted(load)
onBeforeUnmount(stopPolling)
</script>

<style scoped lang="scss">
.kpi-card {
  height: 100%;
}

.kpi-value {
  margin-top: 4px;
  min-height: 3.25rem;
}

.kpi-link {
  display: block;
  text-decoration: none;
  color: inherit;
}
</style>
