<template>
  <div data-testid="panel-portfolio">
    <div v-if="!loading && !loadError && allOwnedDomains.length > 0" class="row items-center q-gutter-sm q-mb-sm">
      <q-toggle v-model="hideExpired" label="Hide expired domains" data-testid="hide-expired-toggle" />
      <div v-if="expiredOwnedCount > 0" class="text-grey-8 text-caption" data-testid="expired-count">
        {{ expiredOwnedCount }} expired domain{{ expiredOwnedCount === 1 ? '' : 's' }}
        {{ hideExpired ? 'hidden' : 'shown' }}
      </div>
      <q-toggle v-model="hideMissing" label="Hide missing domains" data-testid="hide-missing-toggle" />
      <div v-if="missingOwnedCount > 0" class="text-grey-8 text-caption" data-testid="missing-count">
        {{ missingOwnedCount }} missing domain{{ missingOwnedCount === 1 ? '' : 's' }}
        {{ hideMissing ? 'hidden' : 'shown' }}
      </div>
    </div>

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="allOwnedDomains.length === 0"
      skeleton="table"
      @retry="load()"
    >
      <template #empty>
        <div class="text-grey-8" data-testid="portfolio-empty">
          No owned domains yet.
          <NuxtLink to="/connections" data-testid="portfolio-empty-link">Connect a registrar</NuxtLink>
          to sync your portfolio, or add one above.
        </div>
      </template>

      <div v-if="ownedDomains.length === 0" class="text-grey-8" data-testid="portfolio-all-expired">
        <template v-if="missingOwnedCount === 0">
          All {{ expiredOwnedCount }} owned domain{{ expiredOwnedCount === 1 ? ' is' : 's are' }} expired and hidden.
        </template>
        <template v-else-if="expiredOwnedCount === 0">
          All {{ missingOwnedCount }} owned domain{{ missingOwnedCount === 1 ? ' is' : 's are' }} missing and hidden.
        </template>
        <template v-else>
          All {{ allOwnedDomains.length }} owned domains are expired or missing and hidden.
        </template>
        <q-btn
          flat
          no-caps
          dense
          color="primary"
          label="Show all"
          data-testid="show-expired-link"
          @click="hideExpired = false; hideMissing = false"
        />
      </div>
      <DomainTable v-else :rows="ownedDomains" :loading="loading" mode="portfolio" @open="onOpen" />
    </AsyncState>
  </div>
</template>

<script setup lang="ts">
import AsyncState from '~/components/ui/AsyncState.vue'
import DomainTable from '~/components/DomainTable.vue'
import { parseUtc } from '~/composables/useApi'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { DomainOut } from '~/composables/useDomainsApi'

const api = useDomainsApi()

const domains = ref<DomainOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

// Hidden by default: an owned portfolio easily accumulates long-lapsed domains (GoDaddy in
// particular has been seen returning "expires" dates years in the past for domains still listed
// in the account), and those otherwise bury the ones that still matter.
const hideExpired = ref(true)
// A "missing" domain (missing_since set) is a sync artifact, not a status the account chose — a
// registrar/connection stopped reporting it. Defaults hidden.
const hideMissing = ref(true)

const allOwnedDomains = computed(() => domains.value.filter((d) => d.ownership === 'owned'))
const expiredOwnedCount = computed(() => allOwnedDomains.value.filter((d) => isExpired(d.expires_at)).length)
const missingOwnedCount = computed(() => allOwnedDomains.value.filter((d) => d.missing_since !== null).length)
const ownedDomains = computed(() => {
  let list = allOwnedDomains.value
  if (hideExpired.value) list = list.filter((d) => !isExpired(d.expires_at))
  if (hideMissing.value) list = list.filter((d) => d.missing_since === null)
  return list
})

function daysUntil(value: string | null): number | null {
  if (!value) return null
  const target = parseUtc(value).getTime()
  if (Number.isNaN(target)) return null
  return Math.ceil((target - Date.now()) / 86_400_000)
}

// No expires_at (null) is "unknown", never "expired": a domain the registrar reports no date for
// must not vanish behind the hide-expired filter.
function isExpired(value: string | null): boolean {
  const days = daysUntil(value)
  return days !== null && days < 0
}

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

// Bumped by the parent (Add/Import/Sync); refetch in place so the table never collapses to a
// loading spinner just because a sibling action landed.
const refreshTick = inject<Ref<number>>('domains-refresh', ref(0))
watch(refreshTick, () => load(true))

const openDetail = inject<(name: string) => void>('open-domain-detail', () => {})

function onOpen(row: DomainOut) {
  openDetail(row.name)
}

onMounted(() => load())
</script>
