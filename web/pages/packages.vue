<template>
  <q-page class="page-container q-pa-md" data-testid="page-packages">
    <PageHeader title="Packages" subtitle="Shipments found in Gmail, tracked by email and carrier APIs">
      <template #actions>
        <q-btn
          outline
          no-caps
          color="primary"
          icon="mail"
          label="Scan Gmail"
          :loading="scanning"
          :disable="refreshing"
          data-testid="packages-scan"
          @click="onScan"
        />
        <q-btn
          outline
          no-caps
          color="primary"
          icon="refresh"
          label="Refresh"
          :loading="refreshing"
          :disable="scanning"
          data-testid="packages-refresh"
          @click="onRefresh"
        />
        <q-btn
          color="primary"
          no-caps
          icon="add"
          label="Add package"
          data-testid="packages-add"
          @click="addOpen = true"
        />
      </template>
    </PageHeader>

    <div v-if="summary" class="row q-col-gutter-sm q-mb-md" data-testid="packages-summary">
      <div v-for="tile in summaryTiles" :key="tile.key" class="col-6 col-sm-4 col-md">
        <q-card flat bordered class="kpi-card" :data-testid="`packages-summary-${tile.key}`">
          <q-card-section class="q-pa-sm">
            <div class="text-caption text-grey-7">{{ tile.label }}</div>
            <div class="text-h5">{{ tile.value }}</div>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <div v-if="unconfiguredCarriers.length > 0" class="text-caption text-grey-8 q-mb-sm" data-testid="packages-carrier-notice">
      <q-icon name="info" size="xs" class="q-mr-xs" />
      {{ unconfiguredCarriers.map((c) => c.toUpperCase()).join('/') }} API not configured; status comes from email only
    </div>

    <div class="row q-col-gutter-sm q-mb-sm items-center">
      <div class="col-12 col-sm-6 col-md-3">
        <q-select
          v-model="statusFilter"
          dense
          outlined
          emit-value
          map-options
          label="Status"
          :options="statusOptions"
          data-testid="packages-filter-status"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-3">
        <q-select
          v-model="carrierFilter"
          dense
          outlined
          emit-value
          map-options
          label="Carrier"
          :options="carrierOptions"
          data-testid="packages-filter-carrier"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-3">
        <q-input
          v-model="textFilter"
          dense
          outlined
          clearable
          label="Search"
          :debounce="300"
          data-testid="packages-filter-text"
        />
      </div>
      <div class="col-12 col-sm-auto">
        <q-toggle v-model="showArchived" label="Show archived" data-testid="packages-filter-archived" />
      </div>
    </div>

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="packages.length === 0"
      skeleton="table"
      @retry="load()"
    >
      <template #empty>
        <EmptyState
          v-if="!filtersActive"
          icon="local_shipping"
          title="No packages yet"
          message="No packages yet. Scan Gmail or add a tracking number."
          data-testid="packages-empty"
        />
        <EmptyState
          v-else
          icon="filter_alt_off"
          title="No packages match these filters"
          message="Try widening or clearing your filters."
          data-testid="packages-empty-filtered"
        />
      </template>

      <div style="overflow-x: auto">
        <q-table
          v-model:pagination="pagination"
          dense
          flat
          bordered
          :rows="packages"
          :columns="columns"
          row-key="id"
          :rows-per-page-options="[25, 50, 0]"
          data-testid="packages-table"
        >
          <template #body="rowProps">
            <q-tr
              :props="rowProps"
              tabindex="0"
              class="cursor-pointer"
              data-testid="package-row"
              @click="openDetail(rowProps.row)"
              @keyup.enter="openDetail(rowProps.row)"
            >
              <q-td key="status" :props="rowProps">
                <StatusChip kind="package" :value="rowProps.row.status" />
              </q-td>
              <q-td key="carrier" :props="rowProps">{{ carrierLabel(rowProps.row.carrier) }}</q-td>
              <q-td key="tracking_number" :props="rowProps">
                <span v-if="rowProps.row.tracking_number" class="mono">{{ rowProps.row.tracking_number }}</span>
                <span v-else>Order {{ rowProps.row.order_id ?? '—' }}</span>
                <q-btn
                  v-if="rowProps.row.tracking_url"
                  flat
                  round
                  dense
                  size="sm"
                  icon="open_in_new"
                  type="a"
                  :href="rowProps.row.tracking_url"
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label="Open carrier tracking page"
                  data-testid="package-track-link"
                  @click.stop
                  @keyup.enter.stop
                />
              </q-td>
              <q-td key="merchant" :props="rowProps">{{ rowProps.row.merchant ?? '—' }}</q-td>
              <q-td key="last_event" :props="rowProps">
                <div>{{ rowProps.row.last_event ?? '—' }}</div>
                <div v-if="rowProps.row.last_event_at" class="text-caption text-grey-7">
                  {{ relativeTime(rowProps.row.last_event_at) }}
                </div>
              </q-td>
              <q-td key="eta" :props="rowProps">{{ formatDate(rowProps.row.eta) }}</q-td>
              <q-td key="delivered_at" :props="rowProps">{{ formatDate(rowProps.row.delivered_at) }}</q-td>
            </q-tr>
          </template>
        </q-table>
      </div>
    </AsyncState>

    <PackageDetailDialog v-model="detailOpen" :package-id="detailId" @changed="load(true)" />
    <PackageAddDialog v-model="addOpen" @added="load(true)" />
  </q-page>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import AsyncState from '~/components/ui/AsyncState.vue'
import EmptyState from '~/components/ui/EmptyState.vue'
import PageHeader from '~/components/ui/PageHeader.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import PackageDetailDialog from '~/components/PackageDetailDialog.vue'
import PackageAddDialog from '~/components/PackageAddDialog.vue'
import { usePackagesApi } from '~/composables/usePackagesApi'
import type { CarrierStatus, PackageOut, PackageStatus, PackageSummary } from '~/composables/usePackagesApi'
import { formatDate, humanize, relativeTime } from '~/utils/format'
import { errorText } from '~/utils/errors'

const api = usePackagesApi()
const $q = useQuasar()

const packages = ref<PackageOut[]>([])
const summary = ref<PackageSummary | null>(null)
const carriers = ref<CarrierStatus[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const statusFilter = ref<PackageStatus | 'all'>('all')
const carrierFilter = ref('all')
const textFilter = ref<string | null>('')
const showArchived = ref(false)

const scanning = ref(false)
const refreshing = ref(false)
const detailOpen = ref(false)
const detailId = ref<number | null>(null)
const addOpen = ref(false)

const pagination = ref({ rowsPerPage: 25, page: 1 })

const STATUSES: PackageStatus[] = [
  'pending',
  'in_transit',
  'out_for_delivery',
  'delivered',
  'exception',
  'returned',
  'stale',
  'unknown',
]
const statusOptions = [
  { label: 'All statuses', value: 'all' },
  ...STATUSES.map((value) => ({ label: humanize(value), value })),
]

// Carrier options come from rows seen so far (accumulated), so filtering to one carrier does not
// make the other options vanish from the select.
const seenCarriers = ref<string[]>([])
const carrierOptions = computed(() => [
  { label: 'All carriers', value: 'all' },
  ...seenCarriers.value.map((value) => ({ label: carrierLabel(value), value })),
])

const unconfiguredCarriers = computed(() => carriers.value.filter((c) => !c.configured).map((c) => c.code))

const summaryTiles = computed(() => {
  const s = summary.value
  if (!s) return []
  return [
    { key: 'out-for-delivery', label: 'Out for delivery today', value: s.out_for_delivery_today },
    { key: 'arriving', label: 'Arriving today', value: s.arriving_today },
    { key: 'delivered', label: 'Delivered today', value: s.delivered_today },
    { key: 'in-transit', label: 'In transit', value: s.in_transit },
    { key: 'exceptions', label: 'Exceptions', value: s.exceptions },
  ]
})

const filtersActive = computed(
  () =>
    statusFilter.value !== 'all' ||
    carrierFilter.value !== 'all' ||
    (textFilter.value ?? '').trim() !== '' ||
    showArchived.value,
)

const columns: QTableColumn[] = [
  { name: 'status', label: 'Status', field: 'status', align: 'left' },
  { name: 'carrier', label: 'Carrier', field: 'carrier', align: 'left' },
  { name: 'tracking_number', label: 'Tracking #', field: 'tracking_number', align: 'left' },
  { name: 'merchant', label: 'Merchant', field: 'merchant', align: 'left' },
  { name: 'last_event', label: 'Last event', field: 'last_event', align: 'left' },
  { name: 'eta', label: 'ETA', field: 'eta', align: 'left' },
  { name: 'delivered_at', label: 'Delivered', field: 'delivered_at', align: 'left' },
]

function carrierLabel(code: string): string {
  return code.length <= 5 ? code.toUpperCase() : humanize(code)
}

/** Loads the filtered list plus summary and carrier readiness; `silent` keeps the table on screen. */
let loadSeq = 0
async function load(silent = false) {
  const mine = ++loadSeq
  if (!silent) loading.value = true
  loadError.value = null
  try {
    const q = (textFilter.value ?? '').trim()
    const rows = await api.listPackages({
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      carrier: carrierFilter.value === 'all' ? undefined : carrierFilter.value,
      include_archived: showArchived.value ? true : undefined,
      q: q || undefined,
    })
    if (mine !== loadSeq) return
    packages.value = rows
    const known = new Set(seenCarriers.value)
    for (const row of rows) known.add(row.carrier)
    seenCarriers.value = [...known].sort()
    // Summary and carrier notice are secondary: their failure must not hide the list.
    const [s, c] = await Promise.allSettled([api.summary(), api.carriers()])
    if (mine !== loadSeq) return
    if (s.status === 'fulfilled') summary.value = s.value
    if (c.status === 'fulfilled') carriers.value = c.value
  } catch (error) {
    if (mine === loadSeq) loadError.value = errorText(error)
  } finally {
    if (mine === loadSeq) loading.value = false
  }
}

async function onScan() {
  scanning.value = true
  try {
    const results = await api.scan()
    const scanned = results.reduce((n, r) => n + r.scanned, 0)
    const touched = results.reduce((n, r) => n + r.packages_touched, 0)
    $q.notify({ type: 'positive', message: `Scanned ${scanned} emails, ${touched} packages updated` })
    for (const r of results) {
      if (r.errors.length > 0) {
        $q.notify({ type: 'warning', message: `${r.label}: ${r.errors.join('; ')}` })
      }
    }
    await load(true)
  } catch (error) {
    $q.notify({ type: 'negative', message: `Scan failed: ${errorText(error)}` })
  } finally {
    scanning.value = false
  }
}

async function onRefresh() {
  refreshing.value = true
  try {
    const result = await api.refresh()
    $q.notify({ type: 'positive', message: `Checked ${result.polled} packages, ${result.changed} changed` })
    const failures = Object.entries(result.errors)
    if (failures.length > 0) {
      $q.notify({ type: 'warning', message: failures.map(([carrier, message]) => `${carrier}: ${message}`).join('; ') })
    }
    await load(true)
  } catch (error) {
    $q.notify({ type: 'negative', message: `Refresh failed: ${errorText(error)}` })
  } finally {
    refreshing.value = false
  }
}

function openDetail(row: PackageOut) {
  detailId.value = row.id
  detailOpen.value = true
}

watch([statusFilter, carrierFilter, textFilter, showArchived], () => load(true))

onMounted(() => load())
</script>

<style scoped lang="scss">
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
