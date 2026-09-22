<template>
  <q-card data-testid="jobs-companies-panel">
    <q-card-section class="q-gutter-sm">
      <div class="text-h6">Companies</div>

      <div>
        <div class="text-subtitle2">Add company</div>
        <div class="row items-center q-gutter-xs">
          <q-input
            v-model="addName"
            dense
            outlined
            label="Company name"
            data-testid="jobs-company-add-name"
          />
          <q-input
            v-model="addDomain"
            dense
            outlined
            label="Domain (optional)"
            data-testid="jobs-company-add-domain"
          />
          <q-btn
            flat
            dense
            no-caps
            label="Add"
            :loading="adding"
            :disable="!addName.trim()"
            data-testid="jobs-company-add"
            @click="onAddCompany"
          />
        </div>
        <q-banner v-if="addError" dense class="bg-negative text-white" data-testid="jobs-company-add-error">
          {{ addError }}
        </q-banner>
      </div>

      <div>
        <div class="text-subtitle2">CSV import</div>
        <q-input
          v-model="csvInput"
          type="textarea"
          outlined
          dense
          label="CSV (name,domain per line)"
          data-testid="jobs-companies-csv-input"
        />
        <q-btn
          flat
          dense
          no-caps
          label="Import CSV"
          :loading="importingCsv"
          :disable="!csvInput.trim()"
          data-testid="jobs-companies-csv-import"
          @click="onImportCsv"
        />
        <q-banner v-if="csvResultText" dense class="bg-grey-3" data-testid="jobs-companies-csv-result">
          {{ csvResultText }}
        </q-banner>
        <q-banner v-if="csvError" dense class="bg-negative text-white" data-testid="jobs-companies-csv-error">
          {{ csvError }}
        </q-banner>
      </div>

      <div>
        <div class="text-subtitle2">Seed imports</div>
        <div class="row items-center q-gutter-xs">
          <q-btn
            flat
            dense
            no-caps
            label="Import YC"
            :loading="seedLoading === 'yc'"
            data-testid="jobs-import-yc"
            @click="onSeedImport('yc')"
          />
          <q-btn
            flat
            dense
            no-caps
            label="Import HN"
            :loading="seedLoading === 'hn'"
            data-testid="jobs-import-hn"
            @click="onSeedImport('hn')"
          />
          <q-btn
            flat
            dense
            no-caps
            label="Import inbox"
            :loading="seedLoading === 'inbox'"
            data-testid="jobs-import-inbox"
            @click="onSeedImport('inbox')"
          />
        </div>
        <q-banner v-if="seedResultText" dense class="bg-grey-3" data-testid="jobs-import-result">
          {{ seedResultText }}
        </q-banner>
        <q-banner v-if="seedError" dense class="bg-negative text-white" data-testid="jobs-import-error">
          {{ seedError }}
        </q-banner>
      </div>
    </q-card-section>

    <q-separator />

    <q-card-section data-testid="jobs-companies-list">
      <div v-if="loading" data-testid="jobs-companies-loading">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="40%" />
      </div>

      <q-banner v-else-if="loadError" dense class="bg-negative text-white" data-testid="jobs-companies-load-error">
        {{ loadError }}
        <q-btn flat no-caps label="Retry" data-testid="jobs-companies-load-retry" @click="loadCompanies" />
      </q-banner>

      <div v-else-if="companies.length === 0" class="text-grey-8" data-testid="jobs-companies-empty">
        No companies yet. Add one or import from a seed source.
      </div>

      <div v-else>
        <q-list bordered separator>
          <q-item v-for="company in companies" :key="company.id" :data-testid="`jobs-company-${company.id}`">
            <q-item-section>
              <q-item-label>{{ company.name }}</q-item-label>
              <q-item-label caption>{{ company.domain || 'no domain' }}</q-item-label>

              <template v-if="primaryBoard(company)">
                <div class="row items-center q-gutter-xs q-mt-xs">
                  <q-badge color="grey-8" :data-testid="`jobs-company-board-${company.id}`">
                    {{ primaryBoard(company)!.ats_kind }} / {{ primaryBoard(company)!.board_id }}
                  </q-badge>
                  <q-badge outline color="grey-8" :data-testid="`jobs-company-resolved-by-${company.id}`">
                    {{ primaryBoard(company)!.resolved_by }}
                  </q-badge>
                  <q-badge
                    :color="confidenceColor(primaryBoard(company)!.confidence)"
                    :data-testid="`jobs-company-confidence-${company.id}`"
                  >
                    {{ Math.round(primaryBoard(company)!.confidence * 100) }}%
                  </q-badge>
                </div>
                <q-banner
                  v-if="primaryBoard(company)!.last_error"
                  dense
                  class="bg-warning text-black q-mt-xs"
                  :data-testid="`jobs-company-last-error-${company.id}`"
                >
                  {{ primaryBoard(company)!.last_error }}
                </q-banner>
              </template>
              <div v-else class="text-grey-8 q-mt-xs" :data-testid="`jobs-company-unresolved-${company.id}`">
                Unresolved
              </div>
            </q-item-section>

            <q-item-section side>
              <div class="row items-center q-gutter-xs">
                <q-btn
                  flat
                  dense
                  no-caps
                  label="Rescan"
                  :loading="rescanningId === company.id"
                  :data-testid="`jobs-company-rescan-${company.id}`"
                  @click="onRescan(company.id)"
                />
                <q-btn
                  flat
                  dense
                  no-caps
                  label="Override board"
                  data-testid="jobs-board-override"
                  @click="openOverride(company)"
                />
              </div>
              <span
                v-if="rescanErrors[company.id]"
                class="text-negative"
                :data-testid="`jobs-company-rescan-error-${company.id}`"
              >
                {{ rescanErrors[company.id] }}
              </span>
            </q-item-section>
          </q-item>
        </q-list>
      </div>
    </q-card-section>

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
  </q-card>
</template>

<script setup lang="ts">
import { ApiError } from '~/composables/useApi'
import { useJobsApi } from '~/composables/useJobsApi'
import type { BoardOut, CompanyOut } from '~/composables/useJobsApi'

const jobsApi = useJobsApi()

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong'
}

// api/routers/jobs.py resolves boards with these ats_kind values (greenhouse/lever/ashby/workable
// today); the override select mirrors that fixed set rather than accepting free text.
const ATS_KIND_OPTIONS = ['greenhouse', 'lever', 'ashby', 'workable']

// Companies list
const companies = ref<CompanyOut[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

function primaryBoard(company: CompanyOut): BoardOut | null {
  return company.boards.length > 0 ? company.boards[0] : null
}

function confidenceColor(confidence: number): string {
  if (confidence >= 0.75) return 'positive'
  if (confidence >= 0.4) return 'warning'
  return 'negative'
}

async function loadCompanies() {
  loading.value = true
  loadError.value = null
  try {
    companies.value = await jobsApi.listCompanies()
  } catch (err) {
    loadError.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadCompanies()
})

// Add company
const addName = ref('')
const addDomain = ref('')
const adding = ref(false)
const addError = ref<string | null>(null)

async function onAddCompany() {
  const name = addName.value.trim()
  if (!name) return
  adding.value = true
  addError.value = null
  try {
    const created = await jobsApi.addCompany({ name, domain: addDomain.value.trim() || undefined })
    companies.value = [...companies.value, created]
    addName.value = ''
    addDomain.value = ''
  } catch (err) {
    addError.value = errorMessage(err)
  } finally {
    adding.value = false
  }
}

// CSV import
const csvInput = ref('')
const importingCsv = ref(false)
const csvResultText = ref<string | null>(null)
const csvError = ref<string | null>(null)

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
    csvError.value = errorMessage(err)
  } finally {
    importingCsv.value = false
  }
}

// Seed imports
type SeedSource = 'yc' | 'hn' | 'inbox'
const seedLoading = ref<SeedSource | null>(null)
const seedResultText = ref<string | null>(null)
const seedError = ref<string | null>(null)

async function onSeedImport(source: SeedSource) {
  seedLoading.value = source
  seedError.value = null
  seedResultText.value = null
  try {
    const fetcher =
      source === 'yc' ? jobsApi.importCompaniesYc : source === 'hn' ? jobsApi.importCompaniesHn : jobsApi.importCompaniesInbox
    const result = await fetcher()
    seedResultText.value = `${result.added.length} added, ${result.rejected.length} rejected`
    await loadCompanies()
  } catch (err) {
    seedError.value = errorMessage(err)
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
    rescanErrors.value = { ...rescanErrors.value, [id]: errorMessage(err) }
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
    overrideError.value = errorMessage(err)
  } finally {
    overrideSaving.value = false
  }
}
</script>
