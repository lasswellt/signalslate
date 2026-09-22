<template>
  <q-page padding class="q-gutter-md">
    <div class="row items-center q-gutter-sm">
      <div class="text-h5">Jobs</div>
    </div>

    <q-tabs v-model="tab" dense align="left" class="text-grey" active-color="primary" indicator-color="primary">
      <q-tab name="postings" label="Postings" data-testid="tab-postings" />
      <q-tab name="applications" label="Applications" data-testid="tab-applications" />
      <q-tab name="companies" label="Companies" data-testid="tab-companies" />
      <q-tab name="profile" label="Profile" data-testid="tab-profile" />
    </q-tabs>
    <q-separator />

    <div v-if="loading" class="row justify-center q-pa-lg" data-testid="loading">
      <q-spinner size="lg" color="primary" />
    </div>

    <template v-else>
      <!-- Postings and applications load independently (Promise.allSettled), same reasoning as
           domains.vue: one route failing (e.g. a regression on /jobs/applications) must not blank
           out a postings list that loaded fine. -->
      <q-banner v-if="loadError" class="bg-negative text-white" data-testid="load-error">
        {{ loadError }}
        <template #action>
          <q-btn flat label="Retry" @click="load()" />
        </template>
      </q-banner>

      <q-tab-panels v-model="tab" animated>
        <q-tab-panel name="postings" data-testid="panel-postings">
          <div class="row items-center q-gutter-sm q-mb-sm">
            <q-input
              v-model="minScoreFilter"
              dense
              outlined
              type="number"
              label="Min score"
              style="max-width: 140px"
              data-testid="postings-filter-min-score"
            />
            <q-select
              v-model="statusFilter"
              dense
              outlined
              emit-value
              map-options
              label="Status"
              style="min-width: 140px"
              :options="statusOptions"
              data-testid="postings-filter-status"
            />
            <q-select
              v-model="remoteFilter"
              dense
              outlined
              emit-value
              map-options
              label="Remote"
              style="min-width: 140px"
              :options="remoteOptions"
              data-testid="postings-filter-remote"
            />
            <q-input
              v-model="textFilter"
              dense
              outlined
              label="Search title"
              style="min-width: 200px"
              data-testid="postings-filter-text"
            />
          </div>

          <div v-if="sortedPostings.length === 0" class="text-grey-8" data-testid="postings-empty">
            No postings match these filters.
          </div>
          <div v-else style="overflow-x: auto">
            <q-table
              :rows="sortedPostings"
              :columns="postingColumns"
              row-key="id"
              flat
              bordered
              data-testid="postings-table"
            >
              <template #body="rowProps">
                <q-tr
                  :props="rowProps"
                  class="cursor-pointer"
                  :data-testid="`posting-row-${rowProps.row.id}`"
                  @click="openPostingDetail(rowProps.row)"
                >
                  <q-td key="title" :props="rowProps">{{ rowProps.row.title }}</q-td>
                  <q-td key="company_name" :props="rowProps">{{ rowProps.row.company_name }}</q-td>
                  <q-td key="fit_score" :props="rowProps">{{ rowProps.row.fit_score ?? 'Unscored' }}</q-td>
                  <q-td key="status" :props="rowProps">
                    <q-badge :color="rowProps.row.closed_at ? 'grey-7' : 'positive'">
                      {{ rowProps.row.closed_at ? 'Closed' : 'Open' }}
                    </q-badge>
                  </q-td>
                  <q-td key="remote" :props="rowProps">
                    {{ rowProps.row.remote === null ? 'Unknown' : rowProps.row.remote ? 'Yes' : 'No' }}
                  </q-td>
                  <q-td key="location" :props="rowProps">{{ rowProps.row.location ?? 'Unknown' }}</q-td>
                  <q-td key="first_seen" :props="rowProps">{{ formatDate(rowProps.row.first_seen) }}</q-td>
                </q-tr>
              </template>
            </q-table>
          </div>
        </q-tab-panel>

        <q-tab-panel name="applications" data-testid="panel-applications">
          <div v-if="applications.length === 0" class="text-grey-8" data-testid="applications-empty">
            No applications yet.
          </div>
          <div v-else style="overflow-x: auto">
            <q-table
              :rows="applications"
              :columns="applicationColumns"
              row-key="id"
              flat
              bordered
              data-testid="applications-table"
            >
              <template #body="rowProps">
                <q-tr
                  :props="rowProps"
                  class="cursor-pointer"
                  :data-testid="`application-row-${rowProps.row.id}`"
                  @click="openApplyDialog(rowProps.row.id)"
                >
                  <q-td key="id" :props="rowProps">{{ rowProps.row.id }}</q-td>
                  <q-td key="posting_id" :props="rowProps">{{ rowProps.row.posting_id }}</q-td>
                  <q-td key="status" :props="rowProps">
                    <q-badge :color="applicationStatusColor(rowProps.row.status)">{{ rowProps.row.status }}</q-badge>
                  </q-td>
                  <q-td key="assist_state" :props="rowProps">{{ rowProps.row.assist_state }}</q-td>
                  <q-td key="submitted_at" :props="rowProps">{{ formatDate(rowProps.row.submitted_at) }}</q-td>
                </q-tr>
              </template>
            </q-table>
          </div>
        </q-tab-panel>

        <q-tab-panel name="companies" data-testid="panel-companies">
          <JobCompaniesPanel v-if="tab === 'companies'" />
        </q-tab-panel>

        <q-tab-panel name="profile" data-testid="panel-profile">
          <JobsProfilePanel v-if="tab === 'profile'" />
        </q-tab-panel>
      </q-tab-panels>
    </template>

    <JobDetailDialog
      :open="detailOpen"
      :posting="detailPosting"
      @update:open="detailOpen = $event"
      @closed="detailOpen = false"
      @application-created="onApplicationCreated"
    />

    <JobApplyDialog
      v-if="applyApplicationId !== null"
      :open="applyOpen"
      :application-id="applyApplicationId"
      @update:open="applyOpen = $event"
      @closed="applyOpen = false"
      @submitted="onApplicationSubmitted"
    />
  </q-page>
</template>

<script setup lang="ts">
import { ApiError, parseUtc } from '~/composables/useApi'
import { useJobsApi } from '~/composables/useJobsApi'
import type { ApplicationOut, PostingOut } from '~/composables/useJobsApi'

interface PostingColumn {
  name: string
  label: string
  field: string | ((row: PostingOut) => unknown)
  align: 'left' | 'center' | 'right'
}

interface ApplicationColumn {
  name: string
  label: string
  field: string | ((row: ApplicationOut) => unknown)
  align: 'left' | 'center' | 'right'
}

const api = useJobsApi()

const tab = ref<'postings' | 'applications' | 'companies' | 'profile'>('postings')

const postings = ref<PostingOut[]>([])
const applications = ref<ApplicationOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const minScoreFilter = ref('')
const statusFilter = ref<'all' | 'open' | 'closed'>('all')
const remoteFilter = ref<'any' | 'remote' | 'onsite'>('any')
const textFilter = ref('')

const statusOptions = [
  { label: 'All', value: 'all' },
  { label: 'Open', value: 'open' },
  { label: 'Closed', value: 'closed' },
]

const remoteOptions = [
  { label: 'Any', value: 'any' },
  { label: 'Remote', value: 'remote' },
  { label: 'On-site', value: 'onsite' },
]

const postingColumns: PostingColumn[] = [
  { name: 'title', label: 'Title', field: 'title', align: 'left' },
  { name: 'company_name', label: 'Company', field: 'company_name', align: 'left' },
  { name: 'fit_score', label: 'Fit', field: 'fit_score', align: 'left' },
  { name: 'status', label: 'Status', field: () => null, align: 'left' },
  { name: 'remote', label: 'Remote', field: 'remote', align: 'left' },
  { name: 'location', label: 'Location', field: 'location', align: 'left' },
  { name: 'first_seen', label: 'First seen', field: 'first_seen', align: 'left' },
]

const applicationColumns: ApplicationColumn[] = [
  { name: 'id', label: 'Id', field: 'id', align: 'left' },
  { name: 'posting_id', label: 'Posting', field: 'posting_id', align: 'left' },
  { name: 'status', label: 'Status', field: 'status', align: 'left' },
  { name: 'assist_state', label: 'Assist', field: 'assist_state', align: 'left' },
  { name: 'submitted_at', label: 'Submitted', field: 'submitted_at', align: 'left' },
]

// GET /jobs/postings sorts by last_seen desc server-side (api/routers/jobs.py); the task wants
// fit_score desc then first_seen desc, so that ordering is applied client-side on top of whatever
// filtered set the server returns. Unscored postings (fit_score null) sort after every scored one.
const sortedPostings = computed(() =>
  [...postings.value].sort((a, b) => {
    const fitDiff = (b.fit_score ?? -1) - (a.fit_score ?? -1)
    if (fitDiff !== 0) return fitDiff
    return parseUtc(b.first_seen).getTime() - parseUtc(a.first_seen).getTime()
  }),
)

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong'
}

function formatDate(value: string | null): string {
  if (!value) return '—'
  const date = parseUtc(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString()
}

function applicationStatusColor(status: string): string {
  if (status === 'submitted' || status === 'interviewing') return 'positive'
  if (status === 'rejected' || status === 'withdrawn') return 'negative'
  return 'grey-7'
}

async function loadPostings(): Promise<PostingOut[]> {
  return api.listPostings({
    minScore: minScoreFilter.value.trim() ? Number(minScoreFilter.value) : undefined,
    status: statusFilter.value === 'all' ? undefined : statusFilter.value,
    remote: remoteFilter.value === 'any' ? undefined : remoteFilter.value === 'remote',
    text: textFilter.value.trim() || undefined,
  })
}

async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  // Settled, not all: postings and applications are independent lists from independent routes —
  // same reasoning as domains.vue's domains/purchases load.
  const [postingResult, applicationResult] = await Promise.allSettled([loadPostings(), api.listApplications()])
  if (postingResult.status === 'fulfilled') postings.value = postingResult.value
  if (applicationResult.status === 'fulfilled') applications.value = applicationResult.value
  const failed = [postingResult, applicationResult].find((result) => result.status === 'rejected')
  loadError.value = failed ? errorText((failed as PromiseRejectedResult).reason) : null
  loading.value = false
}

async function reloadPostings() {
  try {
    postings.value = await loadPostings()
  } catch (error) {
    loadError.value = errorText(error)
  }
}

watch([minScoreFilter, statusFilter, remoteFilter, textFilter], () => {
  void reloadPostings()
})

const detailOpen = ref(false)
const detailPosting = ref<PostingOut | null>(null)

function openPostingDetail(posting: PostingOut) {
  detailPosting.value = posting
  detailOpen.value = true
}

const applyOpen = ref(false)
const applyApplicationId = ref<number | null>(null)

function openApplyDialog(applicationId: number) {
  applyApplicationId.value = applicationId
  applyOpen.value = true
}

async function reloadApplications() {
  try {
    applications.value = await api.listApplications()
  } catch (error) {
    loadError.value = errorText(error)
  }
}

function onApplicationCreated(application: ApplicationOut) {
  detailOpen.value = false
  openApplyDialog(application.id)
  void reloadApplications()
}

function onApplicationSubmitted() {
  void reloadApplications()
}

onMounted(() => load())
</script>
