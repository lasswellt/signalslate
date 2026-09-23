<template>
  <div data-testid="jobs-companies-panel">
    <div class="row q-col-gutter-sm q-mb-sm items-center">
      <div class="col-12 col-sm-6 col-md-4">
        <q-input
          v-model="search"
          dense
          outlined
          clearable
          :debounce="300"
          label="Search companies"
          data-testid="jobs-companies-search"
        />
      </div>
      <q-space />
      <q-btn
        color="primary"
        no-caps
        label="Add company"
        data-testid="jobs-company-add-open"
        @click="openAdd"
      />
      <q-btn-dropdown color="primary" flat no-caps label="Import" :loading="seedLoading !== null" data-testid="jobs-companies-import">
        <q-list>
          <q-item clickable v-close-popup data-testid="jobs-companies-import-csv" @click="openCsv">
            <q-item-section>From CSV&hellip;</q-item-section>
          </q-item>
          <q-item clickable v-close-popup data-testid="jobs-import-yc" @click="confirmSeedImport('yc')">
            <q-item-section>From Y Combinator</q-item-section>
          </q-item>
          <q-item clickable v-close-popup data-testid="jobs-import-hn" @click="confirmSeedImport('hn')">
            <q-item-section>From Hacker News hiring</q-item-section>
          </q-item>
          <q-item clickable v-close-popup data-testid="jobs-import-inbox" @click="confirmSeedImport('inbox')">
            <q-item-section>From inbox</q-item-section>
          </q-item>
        </q-list>
      </q-btn-dropdown>
    </div>

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="filteredCompanies.length === 0"
      skeleton="table"
      @retry="loadCompanies"
    >
      <template #empty>
        <EmptyState
          v-if="!filtersActive"
          icon="business"
          title="No companies yet"
          message="Add a company or import from a seed source to get started."
        >
          <template #action>
            <q-btn color="primary" no-caps label="Add company" data-testid="jobs-companies-empty-add" @click="openAdd" />
          </template>
        </EmptyState>
        <EmptyState
          v-else
          icon="filter_alt_off"
          title="No companies match this search"
          message="Try a different name or domain."
        >
          <template #action>
            <q-btn flat no-caps color="primary" label="Clear search" data-testid="jobs-companies-clear-search" @click="search = ''" />
          </template>
        </EmptyState>
      </template>

      <div style="overflow-x: auto">
        <q-table
          v-model:pagination="pagination"
          :rows="filteredCompanies"
          :columns="columns"
          row-key="id"
          flat
          bordered
          dense
          :rows-per-page-options="[25, 50, 0]"
          data-testid="jobs-companies-table"
        >
          <template #body="rowProps">
            <q-tr :props="rowProps" :data-testid="`jobs-company-${rowProps.row.id}`">
              <q-td key="name" :props="rowProps">
                <div>{{ rowProps.row.name }}</div>
                <div class="text-caption text-grey-7">{{ rowProps.row.domain || 'No domain' }}</div>
              </q-td>
              <q-td key="board" :props="rowProps">
                <template v-if="primaryBoard(rowProps.row)">
                  {{ humanize(primaryBoard(rowProps.row)!.ats_kind) }}
                  <q-tooltip>Board id: {{ primaryBoard(rowProps.row)!.board_id }}</q-tooltip>
                </template>
                <span v-else class="text-grey-7" :data-testid="`jobs-company-unresolved-${rowProps.row.id}`">Unresolved</span>
              </q-td>
              <q-td key="confidence" :props="rowProps">
                <StatusChip
                  v-if="primaryBoard(rowProps.row)"
                  kind="confidence"
                  :value="primaryBoard(rowProps.row)!.confidence"
                  :label-override="confidenceLabel(primaryBoard(rowProps.row)!.confidence)"
                  :data-testid="`jobs-company-confidence-${rowProps.row.id}`"
                />
                <span v-else class="text-grey-7">&mdash;</span>
              </q-td>
              <q-td key="resolved_by" :props="rowProps">
                {{ primaryBoard(rowProps.row) ? humanize(primaryBoard(rowProps.row)!.resolved_by) : '—' }}
              </q-td>
              <q-td key="last_seen" :props="rowProps">
                {{ relativeTime(rowProps.row.last_seen) }}
                <q-tooltip>{{ formatDate(rowProps.row.last_seen) }}</q-tooltip>
              </q-td>
              <q-td key="actions" :props="rowProps">
                <div class="row items-center q-gutter-xs no-wrap">
                  <q-btn
                    flat
                    dense
                    round
                    icon="refresh"
                    :loading="rescanningId === rowProps.row.id"
                    :aria-label="`Rescan ${rowProps.row.name}`"
                    :data-testid="`jobs-company-rescan-${rowProps.row.id}`"
                    @click="onRescan(rowProps.row.id)"
                  >
                    <q-tooltip>Rescan for a job board</q-tooltip>
                  </q-btn>
                  <q-btn
                    flat
                    dense
                    round
                    icon="edit"
                    :aria-label="`Override board for ${rowProps.row.name}`"
                    :data-testid="`override-${rowProps.row.id}`"
                    @click="openOverride(rowProps.row)"
                  >
                    <q-tooltip>Override board</q-tooltip>
                  </q-btn>
                </div>
                <div
                  v-if="rescanErrors[rowProps.row.id]"
                  class="text-negative text-caption"
                  :data-testid="`jobs-company-rescan-error-${rowProps.row.id}`"
                >
                  {{ rescanErrors[rowProps.row.id] }}
                </div>
                <div
                  v-if="primaryBoard(rowProps.row)?.last_error"
                  class="text-negative text-caption"
                  :data-testid="`jobs-company-last-error-${rowProps.row.id}`"
                >
                  {{ primaryBoard(rowProps.row)!.last_error }}
                </div>
              </q-td>
            </q-tr>
          </template>
        </q-table>
      </div>
    </AsyncState>

    <q-dialog v-model="addOpen">
      <q-card style="min-width: 320px" data-testid="jobs-company-add-dialog">
        <q-card-section>
          <div class="text-h6">Add company</div>
        </q-card-section>
        <q-card-section class="q-gutter-sm">
          <q-input v-model="addName" dense outlined label="Company name" data-testid="jobs-company-add-name" />
          <q-input v-model="addDomain" dense outlined label="Domain (optional)" data-testid="jobs-company-add-domain" />
          <q-banner v-if="addError" dense class="bg-negative text-white" data-testid="jobs-company-add-error">
            {{ addError }}
          </q-banner>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
          <q-btn
            flat
            no-caps
            color="primary"
            label="Add"
            :loading="adding"
            :disable="!addName.trim()"
            data-testid="jobs-company-add"
            @click="onAddCompany"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="csvOpen">
      <q-card style="min-width: 360px" data-testid="jobs-companies-csv-dialog">
        <q-card-section>
          <div class="text-h6">Import from CSV</div>
        </q-card-section>
        <q-card-section class="q-gutter-sm">
          <q-input
            v-model="csvInput"
            type="textarea"
            outlined
            dense
            label="CSV (name,domain per line)"
            data-testid="jobs-companies-csv-input"
          />
          <q-banner v-if="csvResultText" dense class="bg-grey-3" data-testid="jobs-companies-csv-result">
            {{ csvResultText }}
          </q-banner>
          <q-banner v-if="csvError" dense class="bg-negative text-white" data-testid="jobs-companies-csv-error">
            {{ csvError }}
          </q-banner>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Close" v-close-popup />
          <q-btn
            flat
            no-caps
            color="primary"
            label="Import"
            :loading="importingCsv"
            :disable="!csvInput.trim()"
            data-testid="jobs-companies-csv-import"
            @click="onImportCsv"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="overrideOpen">
      <q-card style="min-width: 320px" data-testid="jobs-board-override-dialog">
        <q-card-section>
          <div class="text-h6">Override board</div>
        </q-card-section>
        <q-card-section class="q-gutter-sm">
          <q-input
            v-model="overrideUrl"
            dense
            outlined
            label="Careers URL (optional, for reference only)"
            data-testid="jobs-board-override-url"
          />
          <q-select
            v-model="overrideKind"
            :options="ATS_KIND_OPTIONS"
            dense
            outlined
            label="ATS kind"
            data-testid="jobs-board-override-kind"
          />
          <q-input
            v-model="overrideBoardId"
            dense
            outlined
            label="Board id / slug"
            data-testid="jobs-board-override-board-id"
          />
          <q-banner v-if="overrideError" dense class="bg-negative text-white" data-testid="jobs-board-override-error">
            {{ overrideError }}
          </q-banner>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
          <q-btn
            flat
            no-caps
            color="primary"
            label="Save"
            :loading="overrideSaving"
            :disable="!overrideKind || !overrideBoardId.trim()"
            data-testid="jobs-board-override-save"
            @click="onSaveOverride"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </div>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { useQuasar } from 'quasar'
import { useJobsApi } from '~/composables/useJobsApi'
import type { BoardOut, CompanyOut } from '~/composables/useJobsApi'
import AsyncState from '~/components/ui/AsyncState.vue'
import EmptyState from '~/components/ui/EmptyState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'

const jobsApi = useJobsApi()
const $q = useQuasar()

// api/routers/jobs.py resolves boards with these ats_kind values (greenhouse/lever/ashby/workable
// today); the override select mirrors that fixed set rather than accepting free text.
const ATS_KIND_OPTIONS = ['greenhouse', 'lever', 'ashby', 'workable']

// Companies list
const companies = ref<CompanyOut[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)
const search = ref('')

const filtersActive = computed(() => search.value.trim() !== '')

const filteredCompanies = computed(() => {
  const term = search.value.trim().toLowerCase()
  if (!term) return companies.value
  return companies.value.filter(
    (c) => c.name.toLowerCase().includes(term) || (c.domain ?? '').toLowerCase().includes(term),
  )
})

const pagination = ref({ sortBy: 'name', descending: false, rowsPerPage: 25, page: 1 })

const columns: QTableColumn[] = [
  { name: 'name', label: 'Company', field: 'name', align: 'left', sortable: true },
  { name: 'board', label: 'Job board', field: 'board', align: 'left' },
  { name: 'confidence', label: 'Match confidence', field: 'confidence', align: 'left' },
  { name: 'resolved_by', label: 'Found by', field: 'resolved_by', align: 'left' },
  { name: 'last_seen', label: 'Last seen', field: 'last_seen', align: 'left', sortable: true },
  { name: 'actions', label: 'Actions', field: 'actions', align: 'left' },
]

function primaryBoard(company: CompanyOut): BoardOut | null {
  return company.boards.length > 0 ? company.boards[0] : null
}

/** Combines the confidence tier (High/Medium/Low) with the mapped percent label, e.g. "High (92%)". */
function confidenceLabel(confidence: number): string {
  const tier = confidence >= 0.75 ? 'High' : confidence >= 0.4 ? 'Medium' : 'Low'
  return `${tier} (${statusMeta('confidence', confidence).label})`
}

async function loadCompanies() {
  loading.value = true
  loadError.value = null
  try {
    companies.value = await jobsApi.listCompanies()
  } catch (err) {
    loadError.value = errorText(err)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadCompanies()
})

// Add company
const addOpen = ref(false)
const addName = ref('')
const addDomain = ref('')
const adding = ref(false)
const addError = ref<string | null>(null)

function openAdd() {
  addName.value = ''
  addDomain.value = ''
  addError.value = null
  addOpen.value = true
}

async function onAddCompany() {
  const name = addName.value.trim()
  if (!name) return
  adding.value = true
  addError.value = null
  try {
    const created = await jobsApi.addCompany({ name, domain: addDomain.value.trim() || undefined })
    companies.value = [...companies.value, created]
    addOpen.value = false
  } catch (err) {
    addError.value = errorText(err)
  } finally {
    adding.value = false
  }
}

// CSV import
const csvOpen = ref(false)
const csvInput = ref('')
const importingCsv = ref(false)
const csvResultText = ref<string | null>(null)
const csvError = ref<string | null>(null)

function openCsv() {
  csvInput.value = ''
  csvResultText.value = null
  csvError.value = null
  csvOpen.value = true
}

async function onImportCsv() {
  const csv = csvInput.value.trim()
  if (!csv) return
  importingCsv.value = true
  csvError.value = null
  csvResultText.value = null
  try {
    const result = await jobsApi.importCompanies({ csv })
    csvResultText.value = `${result.added.length} added, ${result.rejected.length} rejected`
    csvInput.value = ''
    await loadCompanies()
  } catch (err) {
    csvError.value = errorText(err)
  } finally {
    importingCsv.value = false
  }
}

// Seed imports. Each asks for confirmation first: these fan out to a third-party source (or the
// user's own inbox) and can issue a non-trivial number of requests.
type SeedSource = 'yc' | 'hn' | 'inbox'
const seedLoading = ref<SeedSource | null>(null)

const SEED_CONFIRM: Record<SeedSource, { title: string; message: string }> = {
  yc: {
    title: 'Import from Y Combinator?',
    message: 'Fetches the current YC company directory and adds any companies not already tracked (roughly 1 request).',
  },
  hn: {
    title: 'Import from Hacker News hiring?',
    message: 'Fetches the latest "Who is hiring?" thread and extracts companies from it (roughly 5-10 requests).',
  },
  inbox: {
    title: 'Import from inbox?',
    message: 'Scans your connected inbox for job-related emails and extracts companies (roughly 10-50 requests, depending on inbox size).',
  },
}

function confirmSeedImport(source: SeedSource) {
  const { title, message } = SEED_CONFIRM[source]
  $q.dialog({
    title,
    message,
    persistent: true,
    cancel: { label: 'Cancel', flat: true, noCaps: true },
    ok: { label: 'Import', color: 'primary', noCaps: true },
  }).onOk(() => {
    void onSeedImport(source)
  })
}

async function onSeedImport(source: SeedSource) {
  seedLoading.value = source
  try {
    const fetcher =
      source === 'yc' ? jobsApi.importCompaniesYc : source === 'hn' ? jobsApi.importCompaniesHn : jobsApi.importCompaniesInbox
    const result = await fetcher()
    $q.notify({ type: 'positive', message: `${result.added.length} added, ${result.rejected.length} rejected` })
    await loadCompanies()
  } catch (err) {
    $q.notify({ type: 'negative', message: errorText(err) })
  } finally {
    seedLoading.value = null
  }
}

// Rescan
const rescanningId = ref<number | null>(null)
const rescanErrors = ref<Record<number, string>>({})

async function onRescan(id: number) {
  rescanningId.value = id
  const nextErrors = { ...rescanErrors.value }
  delete nextErrors[id]
  rescanErrors.value = nextErrors
  try {
    const updated = await jobsApi.rescanCompany(id)
    companies.value = companies.value.map((c) => (c.id === id ? updated : c))
  } catch (err) {
    rescanErrors.value = { ...rescanErrors.value, [id]: errorText(err) }
  } finally {
    rescanningId.value = null
  }
}

// Board override
const overrideOpen = ref(false)
const overrideCompanyId = ref<number | null>(null)
const overrideUrl = ref('')
const overrideKind = ref<string | null>(null)
const overrideBoardId = ref('')
const overrideSaving = ref(false)
const overrideError = ref<string | null>(null)

function openOverride(company: CompanyOut) {
  overrideCompanyId.value = company.id
  const board = primaryBoard(company)
  overrideUrl.value = ''
  overrideKind.value = board?.ats_kind ?? null
  overrideBoardId.value = board?.board_id ?? ''
  overrideError.value = null
  overrideOpen.value = true
}

/** Saves the manual board override. Sends only {ats_kind, board_id} per PUT /jobs/boards/{id}
 * (api/routers/jobs.py) — the pasted careers URL field above is a UX aid for the user to read off
 * the kind/slug from, not something the API accepts directly. */
async function onSaveOverride() {
  if (overrideCompanyId.value === null || !overrideKind.value || !overrideBoardId.value.trim()) return
  overrideSaving.value = true
  overrideError.value = null
  try {
    const board = await jobsApi.overrideBoard(overrideCompanyId.value, {
      ats_kind: overrideKind.value,
      board_id: overrideBoardId.value.trim(),
    })
    companies.value = companies.value.map((c) =>
      c.id === overrideCompanyId.value ? { ...c, boards: [board, ...c.boards.filter((b) => b.id !== board.id)] } : c,
    )
    overrideOpen.value = false
  } catch (err) {
    overrideError.value = errorText(err)
  } finally {
    overrideSaving.value = false
  }
}
</script>
