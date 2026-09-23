<template>
  <div data-testid="panel-purchases">
    <AsyncState :loading="loading" :error="loadError" :empty="purchases.length === 0" skeleton="table" @retry="load()">
      <template #empty>
        <div class="text-grey-8" data-testid="purchases-empty">No purchases yet.</div>
      </template>
      <q-markup-table dense flat bordered data-testid="purchases-table">
        <thead>
          <tr>
            <th class="text-left">Quote</th>
            <th class="text-left">Status</th>
            <th class="text-left">Price</th>
            <th class="text-left">Created</th>
            <th class="text-left">Detail</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="purchase in purchases" :key="purchase.id" data-testid="purchase-row">
            <td>{{ purchase.quote_id }}</td>
            <td>
              <q-badge :color="purchaseStatusColor(purchase.status)" data-testid="purchase-status">{{ purchase.status }}</q-badge>
            </td>
            <td>{{ purchase.price }}</td>
            <td>{{ formatDate(purchase.created_at) }}</td>
            <td>{{ purchase.detail ?? '—' }}</td>
          </tr>
        </tbody>
      </q-markup-table>
    </AsyncState>
  </div>
</template>

<script setup lang="ts">
// Thin move of the former Purchases tab (pages/domains.vue): same table, now with its own
// loading/error state (three-state pattern) since it fetches independently on this route.
import AsyncState from '~/components/ui/AsyncState.vue'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { PurchaseOut } from '~/composables/useDomainsApi'

const api = useDomainsApi()

const purchases = ref<PurchaseOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

function purchaseStatusColor(status: string): string {
  if (status === 'submitted' || status === 'confirmed') return 'positive'
  if (status === 'failed' || status === 'refused') return 'negative'
  return 'grey-7'
}

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
