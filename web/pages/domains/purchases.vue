<template>
  <div data-testid="panel-purchases">
    <div class="row justify-end q-mb-sm">
      <q-btn
        flat
        dense
        no-caps
        icon="refresh"
        label="Refresh"
        :loading="loading"
        data-testid="purchases-refresh"
        @click="load()"
      />
    </div>

    <AsyncState :loading="loading" :error="loadError" :empty="purchases.length === 0" skeleton="table" @retry="load()">
      <template #empty>
        <div class="text-grey-8" data-testid="purchases-empty">No purchases yet.</div>
      </template>
      <q-table
        flat
        bordered
        dense
        row-key="id"
        :rows="purchases"
        :columns="columns"
        :pagination="{ rowsPerPage: 25 }"
        data-testid="purchases-table"
      >
        <template #body-cell-status="cellProps">
          <q-td :props="cellProps">
            <span data-testid="purchase-status">
              <StatusChip kind="purchase" :value="cellProps.row.status" dense />
            </span>
          </q-td>
        </template>
      </q-table>
    </AsyncState>
  </div>
</template>

<script setup lang="ts">
// Thin move of the former Purchases tab (pages/domains.vue): now a q-table with its own loading/error
// state (three-state pattern) since it fetches independently on this route.
import type { QTableColumn } from 'quasar'
import AsyncState from '~/components/ui/AsyncState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { PurchaseOut } from '~/composables/useDomainsApi'

const api = useDomainsApi()

const purchases = ref<PurchaseOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const columns: QTableColumn<PurchaseOut>[] = [
  { name: 'purchase', label: 'Domain', field: (row) => row.name ?? `Purchase #${row.id}`, align: 'left', sortable: true },
  { name: 'price', label: 'Price', field: (row) => Number(row.price), format: (val: number, row: PurchaseOut) => formatMoney(val, row.currency ?? 'USD'), align: 'left', sortable: true },
  { name: 'status', label: 'Status', field: 'status', align: 'left' },
  { name: 'created', label: 'Date', field: 'created_at', format: (val: string | null) => formatDate(val), align: 'left', sortable: true },
]

async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  try {
    purchases.value = await api.listPurchases()
  } catch (error) {
    loadError.value = errorText(error)
  } finally {
    loading.value = false
  }
}

const refreshTick = inject<Ref<number>>('domains-refresh', ref(0))
watch(refreshTick, () => load(true))

onMounted(() => load())
</script>
