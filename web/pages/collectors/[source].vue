<template>
  <q-page class="page-container q-pa-md">
    <PageHeader
      :title="`${humanize(source)} items`"
      :subtitle="`${formatNumber(total)} items collected`"
      back="/collectors"
    />

    <q-banner dense class="bg-warning text-black q-mb-md" data-testid="items-notice">
      Items contain private message content. Do not share what is shown here.
    </q-banner>

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="!loading && allItems.length === 0"
      empty-icon="inbox"
      empty-title="No items yet"
      empty-message="This source has not collected any items yet."
      skeleton="table"
      @retry="load"
    >
      <div class="row items-center q-gutter-sm q-mb-md">
        <q-select
          v-model="selectedType"
          :options="typeOptions"
          emit-value
          map-options
          clearable
          dense
          outlined
          label="Item type"
          style="min-width: 220px"
          data-testid="items-type-filter"
        />
        <q-input
          v-model="search"
          dense
          outlined
          clearable
          label="Search"
          data-testid="items-search"
          style="min-width: 220px"
        />
      </div>

      <div style="overflow-x: auto">
        <q-table
          :rows="rows"
          :columns="columns"
          row-key="id"
          dense
          flat
          bordered
          :pagination="{ sortBy: 'occurred_at', descending: true, rowsPerPage: 25 }"
          data-testid="items-table"
        >
          <template #body="props">
            <q-tr :props="props" class="cursor-pointer" data-testid="item-row" @click="openDetail(props.row as ItemRow)">
              <q-td v-for="col in props.cols" :key="col.name" :props="props">{{ col.value }}</q-td>
            </q-tr>
          </template>
          <template #no-data>
            <div class="full-width text-center text-grey-8 q-pa-md" data-testid="items-empty-filtered">
              No items match these filters.
            </div>
          </template>
        </q-table>
      </div>
    </AsyncState>

    <DialogShell
      :model-value="detailRow !== null"
      :title="detailRow ? `${humanize(detailRow.item_type)} item` : ''"
      :subtitle="detailRow ? formatDate(detailRow.occurred_at) : undefined"
      :busy="detailLoading"
      width="640px"
      @update:model-value="(open: boolean) => { if (!open) closeDetail() }"
    >
      <template v-if="detailLoading">
        <q-skeleton v-for="n in 4" :key="n" type="text" class="q-mb-sm" />
      </template>
      <q-banner v-else-if="detailError" dense class="bg-negative text-white" data-testid="detail-error">
        {{ detailError }}
        <template #action>
          <q-btn flat no-caps label="Retry" @click="retryDetail" />
        </template>
      </q-banner>
      <template v-else-if="detail">
        <div v-if="detail.truncated" class="text-warning q-mb-sm" data-testid="detail-truncated">
          The payload is longer than 20 KB and is cut here.
        </div>
        <div v-if="payloadLines.length === 0" class="text-grey-8 q-mb-sm" data-testid="detail-parse-error">
          Could not read this payload as structured data.
        </div>
        <div v-else class="q-gutter-xs">
          <div
            v-for="(line, i) in payloadLines"
            :key="i"
            data-testid="detail-field"
            :style="{ paddingLeft: `${line.depth * 16}px` }"
          >
            <span class="text-grey-8">{{ line.key }}</span><span v-if="line.value !== ''">: {{ line.value }}</span>
          </div>
        </div>
        <q-expansion-item dense label="Show raw JSON" class="q-mt-md" data-testid="detail-raw-toggle">
          <pre
            class="q-pa-sm bg-grey-2"
            style="white-space: pre-wrap; overflow-wrap: anywhere"
            data-testid="detail-payload"
          >{{ detail.payload }}</pre>
        </q-expansion-item>
      </template>
    </DialogShell>
  </q-page>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import PageHeader from '~/components/ui/PageHeader.vue'
import AsyncState from '~/components/ui/AsyncState.vue'
import DialogShell from '~/components/ui/DialogShell.vue'
import type { ItemDetail, ItemRow } from '~/composables/useApi'

const route = useRoute()
const source = String(route.params.source)
const api = useApi()

// One keyset page is 200 items max (pipeline/db.py MAX_ITEM_PAGE); the type filter and its counts
// need every item up front, so the whole history is paged in once per load. Capped generously: a
// personal-scale collection history fits well inside this many pages.
const PAGE_LIMIT = 200
const MAX_PAGES = 25

const allItems = ref<ItemRow[]>([])
const total = ref(0)
const loading = ref(true)
const loadError = ref<string | null>(null)

const selectedType = ref<string | null>(null)
const search = ref('')

async function fetchAllItems(): Promise<{ items: ItemRow[]; total: number }> {
  const items: ItemRow[] = []
  let beforeId: number | undefined
  let pageTotal = 0
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await api.listItems(source, { limit: PAGE_LIMIT, beforeId })
    items.push(...result.items)
    pageTotal = result.total
    if (result.next_before_id === null) break
    beforeId = result.next_before_id
  }
  return { items, total: pageTotal }
}

async function load() {
  loading.value = true
  loadError.value = null
  try {
    const result = await fetchAllItems()
    allItems.value = result.items
    total.value = result.total
  } catch (error) {
    loadError.value = errorText(error)
  } finally {
    loading.value = false
  }
}

const typeOptions = computed(() => {
  const tally = new Map<string, number>()
  for (const item of allItems.value) tally.set(item.item_type, (tally.get(item.item_type) ?? 0) + 1)
  return [...tally.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([type, count]) => ({ label: `${humanize(type)} (${formatNumber(count)})`, value: type }))
})

const rows = computed(() => {
  const needle = search.value.trim().toLowerCase()
  return allItems.value.filter((item) => {
    if (selectedType.value && item.item_type !== selectedType.value) return false
    if (!needle) return true
    return item.preview.toLowerCase().includes(needle) || humanize(item.item_type).toLowerCase().includes(needle)
  })
})

const columns: QTableColumn[] = [
  {
    name: 'occurred_at',
    label: 'Date',
    field: 'occurred_at',
    align: 'left',
    sortable: true,
    format: (value: string) => formatDate(value),
  },
  {
    name: 'item_type',
    label: 'Type',
    field: 'item_type',
    align: 'left',
    sortable: true,
    format: (value: string) => humanize(value),
  },
  { name: 'preview', label: 'Summary', field: 'preview', align: 'left' },
]

// --- detail ----------------------------------------------------------------------------------

const detailRow = ref<ItemRow | null>(null)
const detail = ref<ItemDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref<string | null>(null)

let detailSession = 0

async function openDetail(row: ItemRow) {
  detailRow.value = row
  detail.value = null
  detailError.value = null
  detailLoading.value = true
  const session = ++detailSession
  try {
    const result = await api.getItem(source, row.id)
    if (session !== detailSession) return
    detail.value = result
  } catch (error) {
    if (session !== detailSession) return
    detailError.value = errorText(error)
  } finally {
    if (session === detailSession) detailLoading.value = false
  }
}

function closeDetail() {
  detailSession += 1
  detailRow.value = null
  detail.value = null
  detailError.value = null
}

async function retryDetail() {
  if (detailRow.value) await openDetail(detailRow.value)
}

interface PayloadLine {
  key: string
  value: string
  depth: number
}

function isIsoLike(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)
}

function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'string') return isIsoLike(value) ? formatDate(value) : value
  return String(value)
}

/** Flattens a parsed payload into indented {key, value} lines; objects/arrays become header rows
 * with their children one level deeper, so the template can render everything with plain divs. */
function flatten(value: unknown, depth: number, label: string, lines: PayloadLine[]): void {
  if (value !== null && typeof value === 'object') {
    const entries = Array.isArray(value)
      ? value.map((item, index) => [`Item ${index + 1}`, item] as const)
      : Object.entries(value as Record<string, unknown>).map(([key, val]) => [humanize(key), val] as const)
    if (entries.length === 0) {
      lines.push({ key: label, value: Array.isArray(value) ? '(empty list)' : '(empty)', depth })
      return
    }
    if (label) lines.push({ key: label, value: '', depth })
    for (const [childLabel, childValue] of entries) {
      flatten(childValue, label ? depth + 1 : depth, childLabel, lines)
    }
    return
  }
  lines.push({ key: label, value: formatScalar(value), depth })
}

const payloadLines = computed<PayloadLine[]>(() => {
  if (!detail.value) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(detail.value.payload)
  } catch {
    return []
  }
  if (parsed === null || typeof parsed !== 'object') return []
  const lines: PayloadLine[] = []
  flatten(parsed, 0, '', lines)
  return lines
})

onMounted(() => load())
</script>
