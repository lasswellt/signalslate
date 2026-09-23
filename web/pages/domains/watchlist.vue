<template>
  <div data-testid="panel-watchlist">
    <div v-if="!loading && !loadError && allWatchedDomains.length > 0" class="row items-center q-gutter-sm q-mb-sm">
      <q-toggle v-model="hideMissing" label="Hide missing domains" data-testid="watchlist-hide-missing-toggle" />
      <div v-if="missingWatchedCount > 0" class="text-grey-8 text-caption" data-testid="watchlist-missing-count">
        {{ missingWatchedCount }} missing domain{{ missingWatchedCount === 1 ? '' : 's' }}
        {{ hideMissing ? 'hidden' : 'shown' }}
      </div>
    </div>

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="allWatchedDomains.length === 0"
      skeleton="table"
      @retry="load()"
    >
      <template #empty>
        <div class="text-grey-8" data-testid="watchlist-empty">
          No watched domains yet. Add one above or generate ideas to watch.
        </div>
      </template>

      <div v-if="watchedDomains.length === 0" class="text-grey-8" data-testid="watchlist-all-missing">
        All {{ missingWatchedCount }} watched domain{{ missingWatchedCount === 1 ? ' is' : 's are' }} missing and hidden.
        <q-btn flat no-caps dense color="primary" label="Show missing" data-testid="show-missing-watchlist-link" @click="hideMissing = false" />
      </div>
      <DomainTable v-else :rows="watchedDomains" :loading="loading" mode="watchlist" @open="onOpen" />
    </AsyncState>
  </div>
</template>

<script setup lang="ts">
import AsyncState from '~/components/ui/AsyncState.vue'
import DomainTable from '~/components/DomainTable.vue'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { DomainOut } from '~/composables/useDomainsApi'

const api = useDomainsApi()

const domains = ref<DomainOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

// A watched domain going past its expiry is often exactly the event being watched for, so unlike
// Portfolio there is no hide-expired toggle here. Missing is still hidden by default (sync artifact).
const hideMissing = ref(true)

const allWatchedDomains = computed(() => domains.value.filter((d) => d.ownership === 'watched'))
const missingWatchedCount = computed(() => allWatchedDomains.value.filter((d) => d.missing_since !== null).length)
const watchedDomains = computed(() =>
  hideMissing.value ? allWatchedDomains.value.filter((d) => d.missing_since === null) : allWatchedDomains.value,
)

async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  try {
    domains.value = await api.listDomains()
  } catch (error) {
    loadError.value = errorText(error)
  } finally {
    loading.value = false
  }
}

const refreshTick = inject<Ref<number>>('domains-refresh', ref(0))
watch(refreshTick, () => load(true))

const openDetail = inject<(name: string) => void>('open-domain-detail', () => {})

function onOpen(row: DomainOut) {
  openDetail(row.name)
}

onMounted(() => load())
</script>
