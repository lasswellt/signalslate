<template>
  <q-page class="page-container q-pa-md">
    <PageHeader :title="pageTitle" back="/history" />

    <AsyncState :loading="loading" :error="loadError" :empty="notFound" @retry="load">
      <template #empty>
        <EmptyState icon="search_off" title="Run not found" message="It may have been deleted, or the link is wrong.">
          <template #action>
            <q-btn flat no-caps color="primary" label="Back to Runs" to="/history" data-testid="run-not-found-back" />
          </template>
        </EmptyState>
      </template>

      <div v-if="run" class="q-gutter-md">
        <q-card>
          <q-card-section>
            <div class="row items-center q-gutter-sm q-mb-md">
              <StatusChip kind="run" :value="run.status" />
              <span class="text-grey-7">{{ triggerLabel(run.trigger) }}</span>
            </div>
            <q-list dense bordered separator>
              <q-item>
                <q-item-section>Duration</q-item-section>
                <q-item-section side data-testid="run-duration">{{ formatDuration(durationMs(run)) }}</q-item-section>
              </q-item>
              <q-item>
                <q-item-section>Finished</q-item-section>
                <q-item-section side data-testid="run-finished">{{ formatDate(run.finished_at) }}</q-item-section>
              </q-item>
            </q-list>
            <div v-if="run.summary" class="q-mt-md" data-testid="run-summary">{{ run.summary }}</div>
            <q-banner v-if="run.error" dense class="bg-negative text-white q-mt-md" data-testid="run-error">
              {{ run.error }}
            </q-banner>
            <q-btn
              v-if="run.has_pdf"
              class="q-mt-md"
              color="primary"
              no-caps
              icon="download"
              label="Download PDF"
              :href="api.pdfUrl(run.id)"
              target="_blank"
              data-testid="download-pdf"
            />
          </q-card-section>
        </q-card>

        <q-card>
          <q-card-section>
            <div class="text-subtitle1 q-mb-sm">Source health for this run</div>
            <HealthList :items="run.source_health" />
          </q-card-section>
        </q-card>
      </div>
    </AsyncState>
  </q-page>
</template>

<script setup lang="ts">
import PageHeader from '~/components/ui/PageHeader.vue'
import AsyncState from '~/components/ui/AsyncState.vue'
import EmptyState from '~/components/ui/EmptyState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import HealthList from '~/components/HealthList.vue'
import { ApiError, parseUtc } from '~/composables/useApi'
import type { RunDetail } from '~/composables/useApi'

// api/routers/runs.py trigger values: manual-source is a single-source run started from Collectors.
const TRIGGER_LABELS: Record<string, string> = {
  manual: 'Manual',
  scheduled: 'Scheduled',
  'manual-source': 'Single source',
}

/** Human label for a run's trigger; unrecognized values fall back to a humanized raw string. */
function triggerLabel(trigger: string): string {
  return TRIGGER_LABELS[trigger] ?? humanize(trigger)
}

/** Wall-clock duration of a run in ms, or null while it has not finished yet. */
function durationMs(row: RunDetail): number | null {
  if (!row.finished_at) return null
  return parseUtc(row.finished_at).getTime() - parseUtc(row.started_at).getTime()
}

const route = useRoute()
const api = useApi()

const rawId = Array.isArray(route.params.id) ? route.params.id[0] : route.params.id
const id = rawId && /^\d+$/.test(rawId) ? Number(rawId) : null

const run = ref<RunDetail | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const notFound = ref(false)

const pageTitle = computed(() => (run.value ? `Run on ${formatDate(run.value.started_at)}` : 'Run'))

/**
 * Loads the run detail for the route's id. A missing/non-numeric id, or a 404 from the API, shows
 * the "Run not found" empty state instead of an error banner.
 * @throws never — failures are caught and surfaced through `loadError` or `notFound`.
 */
async function load() {
  if (id === null) {
    notFound.value = true
    loading.value = false
    return
  }
  loading.value = true
  loadError.value = null
  notFound.value = false
  try {
    run.value = await api.getRun(id)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound.value = true
    else loadError.value = errorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
