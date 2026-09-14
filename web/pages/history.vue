<template>
  <q-page padding class="q-gutter-md" style="max-width: 900px">
    <div class="text-h5">History</div>

    <q-table
      :rows="runs"
      :columns="columns"
      row-key="id"
      flat
      bordered
      :loading="loading"
      @row-click="(_, row) => navigateTo(`/history/${row.id}`)"
      class="cursor-pointer"
    >
      <template #body-cell-status="props">
        <q-td :props="props">
          <q-badge :color="statusColor(props.value)">{{ props.value }}</q-badge>
        </q-td>
      </template>
      <template #body-cell-has_pdf="props">
        <q-td :props="props">
          <q-icon v-if="props.value" name="picture_as_pdf" color="primary" />
          <span v-else class="text-grey">—</span>
        </q-td>
      </template>
    </q-table>
  </q-page>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'

const api = useApi()
const runs = ref<Awaited<ReturnType<typeof api.getRuns>>>([])
const loading = ref(true)

const columns: QTableColumn[] = [
  { name: 'id', label: 'ID', field: 'id', align: 'left' },
  { name: 'trigger', label: 'Trigger', field: 'trigger', align: 'left' },
  { name: 'status', label: 'Status', field: 'status', align: 'left' },
  {
    name: 'started_at',
    label: 'Started',
    field: 'started_at',
    align: 'left',
    format: (v: string) => new Date(v).toLocaleString(),
  },
  { name: 'summary', label: 'Summary', field: 'summary', align: 'left' },
  { name: 'has_pdf', label: 'PDF', field: 'has_pdf', align: 'center' },
]

function statusColor(s: string) {
  return { success: 'positive', partial: 'warning', failed: 'negative', running: 'grey' }[s] ?? 'grey'
}

onMounted(async () => {
  runs.value = await api.getRuns()
  loading.value = false
})
</script>
