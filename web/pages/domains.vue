<template>
  <q-page class="page-container q-pa-md">
    <PageHeader title="Domains" subtitle="Portfolio, watchlist and purchases">
      <template #actions>
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
          icon="refresh"
          label="Sync now"
          :loading="syncing"
          data-testid="domains-sync"
          @click="onSync"
        />
        <q-btn
          flat
          round
          dense
          icon="more_vert"
          aria-label="More domain actions"
          data-testid="domains-more"
        >
          <q-tooltip>More domain actions</q-tooltip>
          <q-menu>
            <q-list style="min-width: 180px">
              <q-item clickable v-close-popup data-testid="domains-import-open" @click="openImport">
                <q-item-section avatar><q-icon name="upload_file" /></q-item-section>
                <q-item-section>Import CSV</q-item-section>
              </q-item>
              <q-item clickable v-close-popup data-testid="domains-inspect-open" @click="openInspect">
                <q-item-section avatar><q-icon name="search" /></q-item-section>
                <q-item-section>Look up a domain</q-item-section>
              </q-item>
            </q-list>
          </q-menu>
        </q-btn>
      </template>
    </PageHeader>

    <q-tabs dense align="left" class="text-grey q-mt-md" active-color="primary" indicator-color="primary">
      <q-route-tab to="/domains" exact label="Portfolio" data-testid="tab-portfolio" />
      <q-route-tab to="/domains/watchlist" label="Watchlist" data-testid="tab-watchlist" />
      <q-route-tab to="/domains/ideas" label="Ideas" data-testid="tab-ideas" />
      <q-route-tab to="/domains/purchases" label="Purchases" data-testid="tab-purchases" />
    </q-tabs>
    <q-separator class="q-mb-md" />

    <NuxtPage />

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
import PageHeader from '~/components/ui/PageHeader.vue'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { ImportOut } from '~/composables/useDomainsApi'

const api = useDomainsApi()
const $q = useQuasar()

const syncing = ref(false)

// Portfolio/Watchlist/Ideas/Purchases each own their own fetch (see pages/domains/*.vue); a change
// here (add/import/sync) bumps this counter so whichever child is currently mounted knows to refetch,
// without the parent holding a domain list of its own. Injection keys are plain strings (not a
// shared composable file) since only this parent and its four child route files use them.
const refreshTick = ref(0)
provide('domains-refresh', refreshTick)

const detailOpen = ref(false)
const detailName = ref('')

function openDetail(name: string) {
  detailName.value = name
  detailOpen.value = true
}

// The detail dialog lives here (so it survives a tab switch); children request it via inject.
provide('open-domain-detail', openDetail)

async function onSync() {
  syncing.value = true
  try {
    const result = await api.syncDomains()
    $q.notify({
      type: 'positive',
      message: `Synced ${result.sync.length} connection(s), refreshed ${result.refresh.length} snapshot(s)`,
    })
    refreshTick.value++
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
    refreshTick.value++
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
    if (result.added.length) refreshTick.value++
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
</script>
