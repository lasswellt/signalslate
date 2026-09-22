<template>
  <q-page padding class="q-gutter-md">
    <div class="row items-center q-gutter-sm">
      <div class="text-h5">Domains</div>
      <q-space />
      <q-btn
        flat
        no-caps
        color="primary"
        icon="refresh"
        label="Sync now"
        :loading="syncing"
        data-testid="domains-sync"
        @click="onSync"
      />
      <q-btn
        color="primary"
        no-caps
        icon="add"
        label="Add domain"
        data-testid="domains-add-open"
        @click="openAdd"
      />
      <q-btn
        flat
        no-caps
        color="primary"
        icon="upload_file"
        label="Import CSV"
        data-testid="domains-import-open"
        @click="openImport"
      />
      <q-btn
        flat
        no-caps
        color="primary"
        icon="search"
        label="Inspect domain"
        data-testid="domains-inspect-open"
        @click="openInspect"
      />
    </div>

    <q-tabs v-model="tab" dense align="left" class="text-grey" active-color="primary" indicator-color="primary">
      <q-tab name="portfolio" label="Portfolio" data-testid="tab-portfolio" />
      <q-tab name="watchlist" label="Watchlist" data-testid="tab-watchlist" />
      <q-tab name="ideas" label="Ideas" data-testid="tab-ideas" />
      <q-tab name="purchases" label="Purchases" data-testid="tab-purchases" />
    </q-tabs>
    <q-separator />

    <div v-if="loading" class="row justify-center q-pa-lg" data-testid="loading">
      <q-spinner size="lg" color="primary" />
    </div>

    <template v-else>
      <!-- Additive, not exclusive with the panels below: domains and purchases load
           independently (Promise.allSettled), so one failing must still show whatever the
           other one got — a banner on top of stale-but-real data, never a blank page hiding it. -->
      <q-banner v-if="loadError" class="bg-negative text-white" data-testid="load-error">
        {{ loadError }}
        <template #action>
          <q-btn flat label="Retry" @click="load()" />
        </template>
      </q-banner>

      <q-tab-panels v-model="tab" animated>
      <q-tab-panel name="portfolio" data-testid="panel-portfolio">
        <div v-if="ownedDomains.length === 0" class="text-grey-8" data-testid="portfolio-empty">
          No owned domains yet.
          <NuxtLink to="/connections" data-testid="portfolio-empty-link">Connect a registrar</NuxtLink>
          to sync your portfolio, or add one above.
        </div>
        <div v-else style="overflow-x: auto">
          <q-table
            :rows="ownedDomains"
            :columns="columns"
            row-key="name"
            flat
            bordered
            data-testid="portfolio-table"
          >
            <template #body="rowProps">
              <q-tr :props="rowProps" class="cursor-pointer" :data-testid="`domain-row-${rowProps.row.name}`" @click="openDetail(rowProps.row.name)">
                <q-td key="name" :props="rowProps">{{ rowProps.row.name }}</q-td>
                <q-td key="source" :props="rowProps">{{ sourceLabel(rowProps.row) }}</q-td>
                <q-td key="expires" :props="rowProps">
                  <span :class="expiryClass(rowProps.row.expires_at)">{{ formatDate(rowProps.row.expires_at) }}</span>
                </q-td>
                <q-td key="auto_renew" :props="rowProps">
                  <q-badge :color="boolColor(rowProps.row.auto_renew)">{{ boolLabel(rowProps.row.auto_renew) }}</q-badge>
                </q-td>
                <q-td key="locked" :props="rowProps">
                  <q-badge :color="boolColor(rowProps.row.locked)">{{ boolLabel(rowProps.row.locked) }}</q-badge>
                </q-td>
                <q-td key="ns_provider" :props="rowProps">
                  <span class="text-grey-8">See details</span>
                </q-td>
                <q-td key="mail" :props="rowProps">
                  <span class="text-grey-8">See details</span>
                </q-td>
                <q-td key="missing_since" :props="rowProps">
                  <span v-if="rowProps.row.missing_since" class="text-negative">{{ formatDate(rowProps.row.missing_since) }}</span>
                  <span v-else class="text-grey-8">&mdash;</span>
                </q-td>
                <q-td key="actions" :props="rowProps" @click.stop>
                  <q-btn
                    flat
                    dense
                    no-caps
                    color="primary"
                    label="Inspect"
                    :data-testid="`domain-inspect-${rowProps.row.name}`"
                    @click="openDetail(rowProps.row.name)"
                  />
                </q-td>
              </q-tr>
            </template>
          </q-table>
        </div>
      </q-tab-panel>

      <q-tab-panel name="watchlist" data-testid="panel-watchlist">
        <div v-if="watchedDomains.length === 0" class="text-grey-8" data-testid="watchlist-empty">
          No watched domains yet. Add one above or generate ideas to watch.
        </div>
        <div v-else style="overflow-x: auto">
          <q-table
            :rows="watchedDomains"
            :columns="columns"
            row-key="name"
            flat
            bordered
            data-testid="watchlist-table"
          >
            <template #body="rowProps">
              <q-tr :props="rowProps" class="cursor-pointer" :data-testid="`domain-row-${rowProps.row.name}`" @click="openDetail(rowProps.row.name)">
                <q-td key="name" :props="rowProps">{{ rowProps.row.name }}</q-td>
                <q-td key="source" :props="rowProps">{{ sourceLabel(rowProps.row) }}</q-td>
                <q-td key="expires" :props="rowProps">
                  <span :class="expiryClass(rowProps.row.expires_at)">{{ formatDate(rowProps.row.expires_at) }}</span>
                </q-td>
                <q-td key="auto_renew" :props="rowProps">
                  <q-badge :color="boolColor(rowProps.row.auto_renew)">{{ boolLabel(rowProps.row.auto_renew) }}</q-badge>
                </q-td>
                <q-td key="locked" :props="rowProps">
                  <q-badge :color="boolColor(rowProps.row.locked)">{{ boolLabel(rowProps.row.locked) }}</q-badge>
                </q-td>
                <q-td key="ns_provider" :props="rowProps">
                  <span class="text-grey-8">See details</span>
                </q-td>
                <q-td key="mail" :props="rowProps">
                  <span class="text-grey-8">See details</span>
                </q-td>
                <q-td key="missing_since" :props="rowProps">
                  <span v-if="rowProps.row.missing_since" class="text-negative">{{ formatDate(rowProps.row.missing_since) }}</span>
                  <span v-else class="text-grey-8">&mdash;</span>
                </q-td>
                <q-td key="actions" :props="rowProps" @click.stop>
                  <q-btn
                    flat
                    dense
                    no-caps
                    color="primary"
                    label="Inspect"
                    :data-testid="`domain-inspect-${rowProps.row.name}`"
                    @click="openDetail(rowProps.row.name)"
                  />
                </q-td>
              </q-tr>
            </template>
          </q-table>
        </div>
      </q-tab-panel>

      <q-tab-panel name="ideas" data-testid="panel-ideas">
        <DomainIdeasPanel v-if="tab === 'ideas'" />
      </q-tab-panel>

      <q-tab-panel name="purchases" data-testid="panel-purchases">
        <div v-if="purchases.length === 0" class="text-grey-8" data-testid="purchases-empty">No purchases yet.</div>
        <q-markup-table v-else dense flat bordered data-testid="purchases-table">
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
      </q-tab-panel>
      </q-tab-panels>
    </template>

    <DomainDetailDialog :open="detailOpen" :name="detailName" @update:open="detailOpen = $event" @closed="detailOpen = false" />

    <q-dialog v-model="addOpen">
      <q-card style="min-width: 320px" data-testid="add-dialog">
        <q-card-section>
          <div class="text-h6">Add domain</div>
        </q-card-section>
        <q-card-section class="q-gutter-sm">
          <q-input
            v-model="addName"
            dense
            outlined
            label="Domain name"
            autofocus
            data-testid="add-name"
            @keyup.enter="submitAdd"
          />
          <q-select
            v-model="addOwnership"
            dense
            outlined
            emit-value
            map-options
            label="Ownership"
            :options="[{ label: 'Owned', value: 'owned' }, { label: 'Watched', value: 'watched' }]"
            data-testid="add-ownership"
          />
          <div v-if="addError" class="text-negative" data-testid="add-error">{{ addError }}</div>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" :disable="addSaving" data-testid="add-cancel" @click="addOpen = false" />
          <q-btn
            color="primary"
            no-caps
            label="Add"
            :loading="addSaving"
            :disable="!addName.trim()"
            data-testid="add-submit"
            @click="submitAdd"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="importOpen">
      <q-card style="min-width: 420px" data-testid="import-dialog">
        <q-card-section>
          <div class="text-h6">Import CSV</div>
          <div class="text-caption text-grey-8">One domain per line, optionally "domain,owned" or "domain,watched". Lines starting with # are skipped.</div>
        </q-card-section>
        <q-card-section>
          <q-input
            v-model="importCsv"
            type="textarea"
            outlined
            rows="8"
            label="Domains"
            data-testid="import-csv"
          />
          <div v-if="importError" class="text-negative q-mt-sm" data-testid="import-error">{{ importError }}</div>
          <div v-if="importResult" class="q-mt-sm" data-testid="import-result">
            <div class="text-positive">Added {{ importResult.added.length }} domain(s)</div>
            <div v-if="importResult.rejected.length" class="text-negative">
              Rejected {{ importResult.rejected.length }} line(s):
              <ul>
                <li v-for="row in importResult.rejected" :key="row.line">Line {{ row.line }}: {{ row.reason }}</li>
              </ul>
            </div>
          </div>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Close" :disable="importSaving" data-testid="import-cancel" @click="closeImport" />
          <q-btn
            color="primary"
            no-caps
            label="Import"
            :loading="importSaving"
            :disable="!importCsv.trim()"
            data-testid="import-submit"
            @click="submitImport"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="inspectOpen">
      <q-card style="min-width: 320px" data-testid="inspect-dialog">
        <q-card-section>
          <div class="text-h6">Inspect domain</div>
        </q-card-section>
        <q-card-section>
          <q-input
            v-model="inspectName"
            dense
            outlined
            label="Domain name"
            autofocus
            data-testid="inspect-name"
            @keyup.enter="submitInspect"
          />
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" data-testid="inspect-cancel" @click="inspectOpen = false" />
          <q-btn
            color="primary"
            no-caps
            label="Inspect"
            :disable="!inspectName.trim()"
            data-testid="inspect-submit"
            @click="submitInspect"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { ApiError, parseUtc } from '~/composables/useApi'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { DomainOut, ImportOut, PurchaseOut } from '~/composables/useDomainsApi'

interface DomainColumn {
  name: string
  label: string
  field: string | ((row: DomainOut) => unknown)
  align: 'left' | 'center' | 'right'
}

const api = useDomainsApi()
const $q = useQuasar()

const tab = ref<'portfolio' | 'watchlist' | 'ideas' | 'purchases'>('portfolio')

const domains = ref<DomainOut[]>([])
const purchases = ref<PurchaseOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)
const syncing = ref(false)

const ownedDomains = computed(() => domains.value.filter((d) => d.ownership === 'owned'))
const watchedDomains = computed(() => domains.value.filter((d) => d.ownership === 'watched'))

// NS provider and mail posture are not returned by GET /api/domains (api/routers/domains.py
// DomainOut): they live on a per-domain snapshot, so the table points at the detail dialog
// (DomainDetailDialog) rather than fetching every row's snapshot up front.
const columns: DomainColumn[] = [
  { name: 'name', label: 'Domain', field: 'name', align: 'left' },
  { name: 'source', label: 'Source / account', field: () => null, align: 'left' },
  { name: 'expires', label: 'Expiry', field: 'expires_at', align: 'left' },
  { name: 'auto_renew', label: 'Auto-renew', field: 'auto_renew', align: 'center' },
  { name: 'locked', label: 'Lock', field: 'locked', align: 'center' },
  { name: 'ns_provider', label: 'NS provider', field: () => null, align: 'left' },
  { name: 'mail', label: 'Mail', field: () => null, align: 'left' },
  { name: 'missing_since', label: 'Missing since', field: 'missing_since', align: 'left' },
  { name: 'actions', label: '', field: () => null, align: 'left' },
]

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong'
}

async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  // Settled, not Promise.all: domains and purchases are independent lists from independent
  // routes. One failing (e.g. a route regression, a transient 5xx) must not discard the other
  // that already succeeded — that is exactly how a real routing bug here once made a fully
  // synced portfolio look empty, because the sibling purchases fetch happened to 404.
  const [domainResult, purchaseResult] = await Promise.allSettled([api.listDomains(), api.listPurchases()])
  if (domainResult.status === 'fulfilled') domains.value = domainResult.value
  if (purchaseResult.status === 'fulfilled') purchases.value = purchaseResult.value
  const failed = [domainResult, purchaseResult].find((result) => result.status === 'rejected')
  loadError.value = failed ? errorText((failed as PromiseRejectedResult).reason) : null
  loading.value = false
}

function sourceLabel(row: DomainOut): string {
  return row.connection_id ? `${row.source} · ${row.connection_id}` : row.source
}

function formatDate(value: string | null): string {
  if (!value) return '—'
  const date = parseUtc(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString()
}

function daysUntil(value: string | null): number | null {
  if (!value) return null
  const target = parseUtc(value).getTime()
  if (Number.isNaN(target)) return null
  return Math.ceil((target - Date.now()) / 86_400_000)
}

function expiryClass(value: string | null): string {
  const days = daysUntil(value)
  return days !== null && days <= 30 ? 'text-negative text-weight-bold' : ''
}

function boolLabel(value: boolean | null): string {
  return value === null ? 'Unknown' : value ? 'Yes' : 'No'
}

function boolColor(value: boolean | null): string {
  return value === null ? 'grey' : value ? 'positive' : 'grey-7'
}

function purchaseStatusColor(status: string): string {
  if (status === 'submitted' || status === 'confirmed') return 'positive'
  if (status === 'failed' || status === 'refused') return 'negative'
  return 'grey-7'
}

async function onSync() {
  syncing.value = true
  try {
    const result = await api.syncDomains()
    $q.notify({
      type: 'positive',
      message: `Synced ${result.sync.length} connection(s), refreshed ${result.refresh.length} snapshot(s)`,
    })
    await load(true)
  } catch (error) {
    $q.notify({ type: 'negative', message: `Sync failed: ${errorText(error)}` })
  } finally {
    syncing.value = false
  }
}

const addOpen = ref(false)
const addName = ref('')
const addOwnership = ref<'owned' | 'watched'>('owned')
const addSaving = ref(false)
const addError = ref<string | null>(null)

function openAdd() {
  addName.value = ''
  addOwnership.value = 'owned'
  addError.value = null
  addOpen.value = true
}

async function submitAdd() {
  const name = addName.value.trim()
  if (!name) return
  addSaving.value = true
  addError.value = null
  try {
    await api.addDomain({ name, ownership: addOwnership.value })
    addOpen.value = false
    $q.notify({ type: 'positive', message: `Added ${name}` })
    await load(true)
  } catch (error) {
    addError.value = errorText(error)
  } finally {
    addSaving.value = false
  }
}

const importOpen = ref(false)
const importCsv = ref('')
const importSaving = ref(false)
const importError = ref<string | null>(null)
const importResult = ref<ImportOut | null>(null)

function openImport() {
  importCsv.value = ''
  importError.value = null
  importResult.value = null
  importOpen.value = true
}

function closeImport() {
  importOpen.value = false
}

async function submitImport() {
  const csv = importCsv.value.trim()
  if (!csv) return
  importSaving.value = true
  importError.value = null
  importResult.value = null
  try {
    const result = await api.importDomains({ csv: importCsv.value })
    importResult.value = result
    if (result.added.length) await load(true)
  } catch (error) {
    importError.value = errorText(error)
  } finally {
    importSaving.value = false
  }
}

const inspectOpen = ref(false)
const inspectName = ref('')

function openInspect() {
  inspectName.value = ''
  inspectOpen.value = true
}

function submitInspect() {
  const name = inspectName.value.trim()
  if (!name) return
  inspectOpen.value = false
  openDetail(name)
}

const detailOpen = ref(false)
const detailName = ref('')

function openDetail(name: string) {
  detailName.value = name
  detailOpen.value = true
}

onMounted(() => load())
</script>
