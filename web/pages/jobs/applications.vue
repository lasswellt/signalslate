<template>
  <div data-testid="panel-applications">
    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="applications.length === 0"
      empty-icon="assignment"
      empty-title="No applications yet"
      empty-message="Start an application from a posting to see it here."
      skeleton="table"
      @retry="load()"
    >
      <div style="overflow-x: auto">
        <q-table
          v-model:pagination="pagination"
          :rows="applications"
          :columns="columns"
          row-key="id"
          flat
          bordered
          :rows-per-page-options="[25, 50, 0]"
          data-testid="applications-table"
        >
          <template #body="rowProps">
            <q-tr
              :props="rowProps"
              tabindex="0"
              class="cursor-pointer"
              :data-testid="`application-row-${rowProps.row.id}`"
              @click="openApply(rowProps.row.id)"
              @keyup.enter="openApply(rowProps.row.id)"
            >
              <q-td key="posting_title" :props="rowProps">
                <span v-if="rowProps.row.posting_title">{{ rowProps.row.posting_title }}</span>
                <span v-else class="text-grey-7 text-caption">Posting #{{ rowProps.row.posting_id }}</span>
              </q-td>
              <q-td key="company_name" :props="rowProps">{{ rowProps.row.company_name ?? '—' }}</q-td>
              <q-td key="status" :props="rowProps">
                <StatusChip kind="application" :value="rowProps.row.status" />
              </q-td>
              <q-td key="assist_state" :props="rowProps">
                <StatusChip kind="assist" :value="rowProps.row.assist_state" />
              </q-td>
              <q-td key="created_at" :props="rowProps">{{ formatDate(rowProps.row.created_at) }}</q-td>
            </q-tr>
          </template>
        </q-table>
      </div>
    </AsyncState>

    <JobApplyDialog
      v-if="applyApplicationId !== null"
      :open="applyOpen"
      :application-id="applyApplicationId"
      @update:open="applyOpen = $event"
      @closed="applyOpen = false"
      @submitted="onSubmitted"
    />
  </div>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import AsyncState from '~/components/ui/AsyncState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import JobApplyDialog from '~/components/JobApplyDialog.vue'
import { useJobsApi } from '~/composables/useJobsApi'
import type { ApplicationOut } from '~/composables/useJobsApi'

const api = useJobsApi()

const applications = ref<ApplicationOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const pagination = ref({ sortBy: 'created_at', descending: true, rowsPerPage: 25, page: 1 })

const columns: QTableColumn[] = [
  { name: 'posting_title', label: 'Job', field: 'posting_title', align: 'left' },
  { name: 'company_name', label: 'Company', field: 'company_name', align: 'left' },
  { name: 'status', label: 'Status', field: 'status', align: 'left' },
  { name: 'assist_state', label: 'Assistant', field: 'assist_state', align: 'left' },
  { name: 'created_at', label: 'Created', field: 'created_at', align: 'left' },
]

async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  try {
    applications.value = await api.listApplications()
  } catch (error) {
    loadError.value = errorText(error, 'Could not load applications')
  } finally {
    loading.value = false
  }
}

const applyOpen = ref(false)
const applyApplicationId = ref<number | null>(null)

function openApply(applicationId: number) {
  applyApplicationId.value = applicationId
  applyOpen.value = true
}

function onSubmitted() {
  void load(true)
}

onMounted(() => load())
</script>
