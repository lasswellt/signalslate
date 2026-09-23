<template>
  <div data-testid="panel-postings">
    <div class="row q-col-gutter-sm q-mb-sm items-center">
      <div class="col-12 col-sm-6 col-md-4">
        <q-input
          v-model="textFilter"
          dense
          outlined
          clearable
          label="Search title"
          :debounce="300"
          data-testid="postings-filter-text"
        />
      </div>
      <div class="col-6 col-sm-3 col-md-2">
        <q-input
          v-model="minScoreFilter"
          dense
          outlined
          type="number"
          :min="0"
          :max="100"
          :debounce="300"
          label="Min fit score"
          data-testid="postings-filter-min-score"
        />
      </div>
      <div class="col-12 col-sm-auto">
        <q-btn-toggle
          v-model="remoteFilter"
          dense
          no-caps
          unelevated
          toggle-color="primary"
          :options="remoteOptions"
          aria-label="Filter by work location"
          data-testid="postings-filter-remote"
        />
      </div>
      <div class="col-12 col-sm-auto">
        <q-btn-toggle
          v-model="statusFilter"
          dense
          no-caps
          unelevated
          toggle-color="primary"
          :options="statusOptions"
          aria-label="Filter by posting status"
          data-testid="postings-filter-status"
        />
      </div>
    </div>

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="postings.length === 0"
      skeleton="table"
      @retry="load()"
    >
      <template #empty>
        <EmptyState
          v-if="!filtersActive"
          icon="work_off"
          title="No openings collected yet"
          message="Add companies to follow to start collecting postings."
          data-testid="postings-empty-none"
        >
          <template #action>
            <q-btn color="primary" no-caps label="Add companies" to="/jobs/companies" data-testid="postings-empty-cta" />
          </template>
        </EmptyState>
        <EmptyState
          v-else
          icon="filter_alt_off"
          title="No openings match these filters"
          message="Try widening or clearing your filters."
          data-testid="postings-empty"
        >
          <template #action>
            <q-btn flat no-caps color="primary" label="Clear filters" data-testid="postings-clear-filters" @click="clearFilters" />
          </template>
        </EmptyState>
      </template>

      <div style="overflow-x: auto">
        <q-table
          v-model:pagination="pagination"
          :rows="postings"
          :columns="columns"
          row-key="id"
          flat
          bordered
          :rows-per-page-options="[25, 50, 0]"
          data-testid="postings-table"
        >
          <template #body="rowProps">
            <q-tr
              :props="rowProps"
              tabindex="0"
              class="cursor-pointer"
              :data-testid="`posting-row-${rowProps.row.id}`"
              @click="openDetail(rowProps.row)"
              @keyup.enter="openDetail(rowProps.row)"
            >
              <q-td key="title" :props="rowProps">
                {{ rowProps.row.title }}
                <q-chip v-if="rowProps.row.closed_at" dense size="sm" icon="lock" label="Closed" class="q-ml-xs" />
              </q-td>
              <q-td key="company_name" :props="rowProps">{{ rowProps.row.company_name }}</q-td>
              <q-td key="fit_score" :props="rowProps">
                <StatusChip kind="fit" :value="rowProps.row.fit_score" />
              </q-td>
              <q-td key="location" :props="rowProps">{{ rowProps.row.location ?? 'Unknown' }}</q-td>
              <q-td key="comp_text" :props="rowProps">{{ rowProps.row.comp_text ?? '—' }}</q-td>
              <q-td key="first_seen" :props="rowProps">
                {{ relativeTime(rowProps.row.first_seen) }}
                <q-tooltip>{{ formatDate(rowProps.row.first_seen) }}</q-tooltip>
              </q-td>
            </q-tr>
          </template>
        </q-table>
      </div>
    </AsyncState>

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
  </div>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import AsyncState from '~/components/ui/AsyncState.vue'
import EmptyState from '~/components/ui/EmptyState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import JobDetailDialog from '~/components/JobDetailDialog.vue'
import JobApplyDialog from '~/components/JobApplyDialog.vue'
import { parseUtc } from '~/composables/useApi'
import { useJobsApi } from '~/composables/useJobsApi'
import type { ApplicationOut, PostingOut } from '~/composables/useJobsApi'

const api = useJobsApi()

const postings = ref<PostingOut[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const textFilter = ref('')
const minScoreFilter = ref('')
const remoteFilter = ref<'any' | 'remote' | 'onsite'>('any')
const statusFilter = ref<'all' | 'open' | 'closed'>('all')

const remoteOptions = [
  { label: 'Any location', value: 'any', attrs: { 'data-testid': 'postings-filter-remote-any' } },
  { label: 'Remote', value: 'remote', attrs: { 'data-testid': 'postings-filter-remote-remote' } },
  { label: 'On-site', value: 'onsite', attrs: { 'data-testid': 'postings-filter-remote-onsite' } },
]
const statusOptions = [
  { label: 'All', value: 'all', attrs: { 'data-testid': 'postings-filter-status-all' } },
  { label: 'Open', value: 'open', attrs: { 'data-testid': 'postings-filter-status-open' } },
  { label: 'Closed', value: 'closed', attrs: { 'data-testid': 'postings-filter-status-closed' } },
]

// Whether any filter is currently narrowing the list — distinguishes "nothing collected yet" from
// "nothing matches these filters" without a second, unfiltered request.
const filtersActive = computed(
  () => textFilter.value.trim() !== '' || minScoreFilter.value.trim() !== '' || remoteFilter.value !== 'any' || statusFilter.value !== 'all',
)

function clearFilters() {
  textFilter.value = ''
  minScoreFilter.value = ''
  remoteFilter.value = 'any'
  statusFilter.value = 'all'
}

const pagination = ref({ sortBy: 'fit_score', descending: true, rowsPerPage: 25, page: 1 })

// Custom sort for fit_score: unscored (null) postings sort after every scored one, and ties break by
// first_seen — same ordering as the previous computed sortedPostings, now expressed as a QTable
// column sort so users can also click "First seen" to sort by that column alone.
function sortByFit(a: number | null, b: number | null, rowA: PostingOut, rowB: PostingOut): number {
  const fitDiff = (a ?? -1) - (b ?? -1)
  if (fitDiff !== 0) return fitDiff
  return parseUtc(rowA.first_seen).getTime() - parseUtc(rowB.first_seen).getTime()
}

const columns: QTableColumn[] = [
  { name: 'title', label: 'Title', field: 'title', align: 'left' },
  { name: 'company_name', label: 'Company', field: 'company_name', align: 'left' },
  { name: 'fit_score', label: 'Fit', field: 'fit_score', align: 'left', sortable: true, sort: sortByFit },
  { name: 'location', label: 'Location', field: 'location', align: 'left' },
  { name: 'comp_text', label: 'Comp', field: 'comp_text', align: 'left' },
  { name: 'first_seen', label: 'First seen', field: 'first_seen', align: 'left', sortable: true },
]

async function fetchPostings(): Promise<PostingOut[]> {
  return api.listPostings({
    minScore: minScoreFilter.value.trim() ? Number(minScoreFilter.value) : undefined,
    remote: remoteFilter.value === 'any' ? undefined : remoteFilter.value === 'remote',
    status: statusFilter.value === 'all' ? undefined : statusFilter.value,
    text: textFilter.value.trim() || undefined,
  })
}

async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  try {
    postings.value = await fetchPostings()
  } catch (error) {
    loadError.value = errorText(error, 'Could not load postings')
  } finally {
    loading.value = false
  }
}

// Filters re-fetch (server-side filtering); the text input's own :debounce="300" already delays
// this watch firing while the user is still typing.
watch([textFilter, minScoreFilter, remoteFilter, statusFilter], () => {
  void load(true)
})

const detailOpen = ref(false)
const detailPosting = ref<PostingOut | null>(null)

function openDetail(posting: PostingOut) {
  detailPosting.value = posting
  detailOpen.value = true
}

const applyOpen = ref(false)
const applyApplicationId = ref<number | null>(null)

function openApplyDialog(applicationId: number) {
  applyApplicationId.value = applicationId
  applyOpen.value = true
}

function onApplicationCreated(application: ApplicationOut) {
  detailOpen.value = false
  openApplyDialog(application.id)
}

function onApplicationSubmitted() {
  // Nothing to refresh here: the postings list is unaffected by an application's status.
}

onMounted(() => load())
</script>
