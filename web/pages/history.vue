<template>
  <q-page class="page-container q-pa-md">
    <PageHeader title="Runs" subtitle="Every digest run, newest first" />

    <AsyncState :loading="loading" :error="loadError" :empty="runs.length === 0" skeleton="table" @retry="load">
      <template #empty>
        <EmptyState icon="history" title="No runs yet" message="Trigger a run from the Overview page to see it here.">
          <template #action>
            <q-btn color="primary" no-caps label="Go to Overview" to="/" data-testid="history-empty-cta" />
          </template>
        </EmptyState>
      </template>

      <div style="overflow-x: auto">
        <q-table
          v-model:pagination="pagination"
          :rows="runs"
          :columns="columns"
          row-key="id"
          flat
          bordered
          :rows-per-page-options="[25, 50, 0]"
          data-testid="runs-table"
        >
          <template #body="rowProps">
            <q-tr
              :props="rowProps"
              tabindex="0"
              class="cursor-pointer"
              data-testid="run-row"
              @click="goToRun(rowProps.row.id)"
              @keyup.enter="goToRun(rowProps.row.id)"
            >
              <q-td key="started_at" :props="rowProps">
                {{ formatDate(rowProps.row.started_at) }}
                <q-tooltip>{{ relativeTime(rowProps.row.started_at) }}</q-tooltip>
              </q-td>
              <q-td key="status" :props="rowProps">
                <StatusChip kind="run" :value="rowProps.row.status" />
              </q-td>
              <q-td key="trigger" :props="rowProps">{{ triggerLabel(rowProps.row.trigger) }}</q-td>
              <q-td key="duration" :props="rowProps">{{ formatDuration(durationMs(rowProps.row)) }}</q-td>
              <q-td key="summary" :props="rowProps" class="ellipsis" style="max-width: 320px">
                <template v-if="rowProps.row.summary">
                  {{ rowProps.row.summary }}
                  <q-tooltip>{{ rowProps.row.summary }}</q-tooltip>
                </template>
                <span v-else class="text-grey-7">&mdash;</span>
              </q-td>
              <q-td key="has_pdf" :props="rowProps" @click.stop>
                <q-btn
                  v-if="rowProps.row.has_pdf"
                  flat
                  dense
                  round
                  icon="picture_as_pdf"
                  color="primary"
                  :aria-label="`Download PDF for run of ${formatDate(rowProps.row.started_at)}`"
                  :href="api.pdfUrl(rowProps.row.id)"
                  target="_blank"
                  data-testid="run-pdf"
                >
                  <q-tooltip>{{ `Download PDF for run of ${formatDate(rowProps.row.started_at)}` }}</q-tooltip>
                </q-btn>
                <span v-else class="text-grey-7">&mdash;</span>
              </q-td>
            </q-tr>
          </template>
        </q-table>
      </div>
    </AsyncState>
  </q-page>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import PageHeader from '~/components/ui/PageHeader.vue'
import AsyncState from '~/components/ui/AsyncState.vue'
import EmptyState from '~/components/ui/EmptyState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import { parseUtc } from '~/composables/useApi'
import type { RunSummary } from '~/composables/useApi'

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
function durationMs(row: RunSummary): number | null {
  if (!row.finished_at) return null
  return parseUtc(row.finished_at).getTime() - parseUtc(row.started_at).getTime()
}

const api = useApi()
const router = useRouter()
const runs = ref<RunSummary[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const pagination = ref({ sortBy: 'started_at', descending: true, rowsPerPage: 25, page: 1 })

const columns: QTableColumn[] = [
  { name: 'started_at', label: 'Started', field: 'started_at', align: 'left', sortable: true },
  { name: 'status', label: 'Status', field: 'status', align: 'left', sortable: true },
  { name: 'trigger', label: 'Trigger', field: 'trigger', align: 'left' },
  { name: 'duration', label: 'Duration', field: (row: RunSummary) => durationMs(row) ?? -1, align: 'left', sortable: true },
  { name: 'summary', label: 'Summary', field: 'summary', align: 'left' },
  { name: 'has_pdf', label: 'PDF', field: 'has_pdf', align: 'center' },
]

/**
 * Loads the runs list. Failures leave the error state up and never leave `loading` stuck.
 * @throws never — failures are caught and surfaced through `loadError`.
 */
async function load() {
  loading.value = true
  loadError.value = null
  try {
    runs.value = await api.getRuns()
  } catch (e) {
    loadError.value = errorText(e)
  } finally {
    loading.value = false
  }
}

function goToRun(id: number) {
  router.push(`/history/${id}`)
}

onMounted(load)
</script>
