<template>
  <q-page class="page-container q-pa-md">
    <PageHeader title="Collectors" subtitle="Each source SignalSlate collects from" />

    <AsyncState
      :loading="loading"
      :error="loadError"
      :empty="collectors.length === 0"
      empty-icon="cloud_sync"
      empty-title="No collectors are declared"
      skeleton="cards"
      @retry="load()"
    >
      <q-card
        v-for="c in collectors"
        :key="c.source"
        flat
        bordered
        class="q-mb-sm"
        data-testid="collector"
        :data-source="c.source"
      >
        <q-card-section>
          <div class="row items-center no-wrap q-gutter-sm">
            <div class="col-grow" style="min-width: 0">
              <div class="text-subtitle1 ellipsis" data-testid="collector-source">{{ humanize(c.source) }}</div>
            </div>
            <StatusChip
              kind="run"
              :value="c.last_attempt?.status ?? null"
              :label-override="c.last_attempt ? undefined : 'No runs yet'"
              dense
              data-testid="collector-status"
            />
          </div>

          <div class="text-body2 text-grey-8 q-mt-sm" data-testid="collector-watermark">
            <template v-if="c.watermark">Collected up to {{ formatDate(c.watermark) }}</template>
            <template v-else>Nothing collected yet</template>
            <q-icon name="info" size="16px" class="q-ml-xs" />
            <q-tooltip>{{ WATERMARK_TOOLTIP }}</q-tooltip>
          </div>

          <div class="text-body2 q-mt-xs" data-testid="collector-attempt">
            <template v-if="c.last_attempt">
              Last run {{ relativeTime(c.last_attempt.at) }}
              <span v-if="c.last_attempt.detail" class="text-grey-8 q-ml-xs" data-testid="attempt-detail">{{ c.last_attempt.detail }}</span>
            </template>
            <template v-else>No runs yet</template>
          </div>

          <div class="text-body2 text-grey-8 q-mt-xs" data-testid="collector-items">
            {{ formatNumber(c.item_count) }} items collected
          </div>

          <div v-if="c.consecutive_failures > 0" class="row items-center q-gutter-xs q-mt-sm">
            <q-chip dense color="warning" text-color="black" icon="warning" data-testid="streak-warning">
              Failing: {{ c.consecutive_failures }} of {{ c.stuck_threshold }} attempts before pause
            </q-chip>
            <q-icon name="info" size="16px" class="text-grey-6" />
            <q-tooltip>
              After {{ c.stuck_threshold }} attempts in a row, SignalSlate moves on: whatever could not be
              collected is skipped, not retried.
            </q-tooltip>
          </div>

          <div v-if="toggleErrors[c.source]" class="q-mt-sm text-negative" data-testid="toggle-error">
            {{ toggleErrors[c.source] }}
          </div>
        </q-card-section>

        <q-card-actions class="row items-center q-gutter-sm">
          <q-btn
            flat
            no-caps
            color="primary"
            icon="play_arrow"
            label="Run now"
            :loading="running[c.source] === true"
            data-testid="collector-run"
            @click="confirmRun(c)"
          />
          <q-btn
            flat
            no-caps
            color="primary"
            icon="inbox"
            label="Items"
            data-testid="collector-browse"
            @click="openItems(c)"
          />
          <q-space />
          <q-toggle
            :model-value="c.active"
            :disable="toggling"
            label="Active"
            data-testid="collector-active"
            @update:model-value="(value: boolean) => onToggle(c, value)"
          />
          <q-btn
            flat
            round
            dense
            icon="more_vert"
            :aria-label="`More actions for ${humanize(c.source)}`"
            data-testid="collector-menu"
          >
            <q-tooltip>{{ `More actions for ${humanize(c.source)}` }}</q-tooltip>
            <q-menu>
              <q-list style="min-width: 220px">
                <q-item clickable v-close-popup :data-testid="`action-dry-run-${c.source}`" @click="openDryRun(c)">
                  <q-item-section avatar><q-icon name="science" /></q-item-section>
                  <q-item-section>Preview (dry run)</q-item-section>
                </q-item>
                <q-item clickable v-close-popup :data-testid="`action-reset-${c.source}`" @click="openReset(c)">
                  <q-item-section avatar><q-icon name="history" /></q-item-section>
                  <q-item-section>Start over from…</q-item-section>
                </q-item>
                <q-item
                  clickable
                  v-close-popup
                  :disable="c.consecutive_failures === 0"
                  :data-testid="`action-clear-${c.source}`"
                  @click="confirmClear(c)"
                >
                  <q-item-section avatar><q-icon name="healing" /></q-item-section>
                  <q-item-section>Clear failures</q-item-section>
                </q-item>
              </q-list>
            </q-menu>
          </q-btn>
        </q-card-actions>
      </q-card>
    </AsyncState>

    <!-- Dry run -->
    <q-dialog :model-value="dryOpen" @update:model-value="(open: boolean) => { if (!open) closeDryRun() }">
      <q-card style="min-width: 360px; max-width: 640px; width: 100%" data-testid="dry-dialog">
        <q-card-section>
          <div class="text-h6">Preview: {{ drySource ? humanize(drySource) : '' }}</div>
          <div class="text-grey-8">Reads from the source and stores nothing.</div>
        </q-card-section>

        <q-card-section class="q-gutter-md">
          <template v-if="dryStage === 'form'">
            <q-input
              v-model.number="dryHours"
              type="number"
              label="Hours back"
              :hint="`1 to ${DRY_MAX_HOURS}`"
              outlined
              dense
              data-testid="dry-hours"
            />
            <q-input
              v-model.number="dryLimit"
              type="number"
              label="Items to list"
              :hint="`0 to ${DRY_MAX_LIMIT}`"
              outlined
              dense
              data-testid="dry-limit"
            />
          </template>

          <div v-else-if="dryStage === 'running'" class="row items-center q-gutter-sm" data-testid="dry-running">
            <q-spinner size="sm" color="primary" />
            <span>Collecting. This can take a while.</span>
          </div>

          <template v-else-if="dryStage === 'done' && dryResult">
            <div class="row items-center q-gutter-sm" data-testid="dry-summary">
              <StatusChip kind="run" :value="dryResult.status" dense />
              <span>{{ dryResult.detail }}</span>
            </div>
            <div class="text-grey-8" data-testid="dry-window">
              {{ dryResult.window.hours }}-hour window: {{ formatDate(dryResult.window.since) }} to
              {{ formatDate(dryResult.window.until) }} · {{ formatDuration(dryResult.duration_ms) }}
            </div>
            <div data-testid="dry-count">{{ formatNumber(dryResult.count) }} items</div>
            <div v-if="Object.keys(dryResult.by_type).length">
              <q-chip v-for="(count, type) in dryResult.by_type" :key="type" dense data-testid="dry-type">
                {{ humanize(String(type)) }}: {{ formatNumber(count) }}
              </q-chip>
            </div>
            <div v-if="!dryResult.items.length" class="text-grey" data-testid="dry-no-items">No items listed</div>
            <q-list v-else separator bordered>
              <q-item v-for="(item, index) in dryResult.items" :key="`${item.external_id}-${index}`" data-testid="dry-item">
                <q-item-section>
                  <q-item-label caption>{{ humanize(item.item_type) }} · {{ formatDate(item.occurred_at) }}</q-item-label>
                  <q-item-label style="white-space: pre-wrap; overflow-wrap: anywhere">{{ item.preview }}</q-item-label>
                </q-item-section>
              </q-item>
            </q-list>
            <template v-if="dryRaw !== null">
              <q-toggle v-model="showRaw" label="Show raw" data-testid="dry-raw-toggle" />
              <pre v-if="showRaw" class="q-pa-sm bg-grey-2" style="white-space: pre-wrap; overflow-wrap: anywhere" data-testid="dry-raw">{{ dryRaw }}</pre>
            </template>
          </template>

          <q-banner v-if="dryError" dense class="bg-negative text-white" data-testid="dry-error">{{ dryError }}</q-banner>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat no-caps label="Close" data-testid="dry-close" @click="closeDryRun" />
          <q-btn
            v-if="dryStage === 'form'"
            color="primary"
            no-caps
            label="Start"
            :disable="!dryValid"
            data-testid="dry-start"
            @click="onStartDryRun"
          />
          <q-btn
            v-else-if="dryStage === 'done' || dryStage === 'failed'"
            color="primary"
            no-caps
            label="Run again"
            data-testid="dry-again"
            @click="dryAgain"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <!-- Start over (reset) -->
    <q-dialog :model-value="resetSource !== null" @update:model-value="(open: boolean) => { if (!open) closeReset() }">
      <q-card style="min-width: 360px; max-width: 560px; width: 100%" data-testid="reset-dialog">
        <q-card-section>
          <div class="text-h6">Start over: {{ resetSource ? humanize(resetSource) : '' }}</div>
        </q-card-section>

        <q-card-section class="q-gutter-sm">
          <template v-if="resetOutcome === null">
            <q-expansion-item dense label="What does this mean?" data-testid="reset-note-toggle">
              <div class="text-body2 q-pa-sm" data-testid="reset-note-static">{{ RESET_NOTE_STATIC }}</div>
            </q-expansion-item>
            <div class="column q-mt-sm">
              <q-radio
                v-for="option in RESET_OPTIONS"
                :key="option.key"
                v-model="resetChoice"
                :val="option.value"
                :label="option.label"
                :disable="resetting"
                :data-testid="`reset-choice-${option.key}`"
              />
            </div>
            <div v-if="resetChoice === null" class="text-grey-8" data-testid="reset-none-hint">
              Starting over from scratch also clears the failure count.
            </div>
          </template>

          <template v-else>
            <div data-testid="reset-result">
              <template v-if="resetOutcome.watermark">Collecting now starts from {{ formatDate(resetOutcome.watermark) }}.</template>
              <template v-else>Collecting will start from scratch next run.</template>
            </div>
            <div class="text-body2" data-testid="reset-note">{{ resetOutcome.note }}</div>
          </template>

          <q-banner v-if="resetError" dense class="bg-negative text-white" data-testid="reset-error">{{ resetError }}</q-banner>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn
            flat
            no-caps
            :label="resetOutcome === null ? 'Cancel' : 'Close'"
            :disable="resetting"
            data-testid="reset-close"
            @click="closeReset"
          />
          <q-btn
            v-if="resetOutcome === null"
            color="negative"
            no-caps
            label="Start over"
            :loading="resetting"
            data-testid="reset-confirm"
            @click="onReset"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <!-- Items -->
    <q-dialog :model-value="itemsSource !== null" @update:model-value="(open: boolean) => { if (!open) closeItems() }">
      <q-card style="min-width: 360px; max-width: 760px; width: 100%" data-testid="items-dialog">
        <q-card-section>
          <div class="text-h6">Items: {{ itemsSource ? humanize(itemsSource) : '' }}</div>
          <q-banner dense class="bg-warning text-black q-mt-sm" data-testid="items-notice">
            Items contain private message content. Do not share what is shown here.
          </q-banner>
        </q-card-section>

        <q-card-section v-if="detailRow === null" class="q-gutter-sm">
          <q-form class="row items-center q-gutter-sm" autocomplete="off" @submit="applyFilter">
            <q-input
              v-model="filterText"
              label="Item type"
              outlined
              dense
              clearable
              data-testid="items-filter"
            />
            <q-btn type="submit" flat no-caps color="primary" label="Filter" data-testid="items-filter-apply" />
          </q-form>

          <div v-if="itemsLoading" class="row justify-center q-pa-md" data-testid="items-loading">
            <q-spinner size="md" color="primary" />
          </div>
          <q-banner v-else-if="itemsError && !items.length" dense class="bg-negative text-white" data-testid="items-error">
            {{ itemsError }}
            <template #action>
              <q-btn flat label="Retry" @click="loadItems(true)" />
            </template>
          </q-banner>
          <div v-else-if="!items.length" class="text-grey" data-testid="items-empty">No items</div>
          <template v-else>
            <div class="text-grey-8" data-testid="items-total">Showing {{ items.length }} of {{ itemsTotal }}</div>
            <q-list separator bordered>
              <q-item v-for="item in items" :key="item.id" clickable data-testid="item-row" @click="openItem(item)">
                <q-item-section>
                  <q-item-label caption>{{ humanize(item.item_type) }} · {{ formatDate(item.occurred_at) }}</q-item-label>
                  <q-item-label style="white-space: pre-wrap; overflow-wrap: anywhere">{{ item.preview }}</q-item-label>
                </q-item-section>
              </q-item>
            </q-list>
            <div v-if="itemsError" class="text-negative" data-testid="items-more-error">{{ itemsError }}</div>
            <div v-if="nextBeforeId !== null" class="row justify-center">
              <q-btn
                flat
                no-caps
                color="primary"
                label="Load more"
                :loading="itemsMoreLoading"
                data-testid="items-more"
                @click="loadItems(false)"
              />
            </div>
          </template>
        </q-card-section>

        <q-card-section v-else class="q-gutter-sm">
          <div class="row items-center q-gutter-sm">
            <q-btn flat dense no-caps icon="arrow_back" label="Back to the list" data-testid="detail-back" @click="closeDetail" />
            <span class="text-grey-8">{{ humanize(detailRow.item_type) }} · {{ formatDate(detailRow.occurred_at) }}</span>
          </div>
          <div v-if="detailLoading" class="row justify-center q-pa-md" data-testid="detail-loading">
            <q-spinner size="md" color="primary" />
          </div>
          <q-banner v-else-if="detailError" dense class="bg-negative text-white" data-testid="detail-error">
            {{ detailError }}
          </q-banner>
          <template v-else-if="detail">
            <div v-if="detail.truncated" class="text-warning" data-testid="detail-truncated">
              The payload is longer than 20 KB and is cut here.
            </div>
            <pre class="q-pa-sm bg-grey-2" style="white-space: pre-wrap; overflow-wrap: anywhere" data-testid="detail-payload">{{ detail.payload }}</pre>
          </template>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat no-caps label="Close" data-testid="items-close" @click="closeItems" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import PageHeader from '~/components/ui/PageHeader.vue'
import AsyncState from '~/components/ui/AsyncState.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import { ApiError } from '~/composables/useApi'
import type { CollectorState, DryRunResult, ItemDetail, ItemRow, ResetResult } from '~/composables/useApi'

const api = useApi()
const $q = useQuasar()

const collectors = ref<CollectorState[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const running = reactive<Record<string, boolean>>({})
const toggleErrors = reactive<Record<string, string | undefined>>({})
// One flag for every toggle: active_sources is a single document that each toggle reads, edits and
// writes back, so two in flight at once would let the later write drop the earlier change.
const toggling = ref(false)

const RUN_POLL_MS = 3000
const RUN_TIMEOUT_MS = 10 * 60_000
const RUN_MAX_POLL_FAILURES = 3
const DRY_POLL_MS = 2000
// Mirror pipeline/collect.py MAX_HOURS / MAX_LIMIT; the API rejects anything larger.
const DRY_MAX_HOURS = 168
const DRY_MAX_LIMIT = 50
const ITEMS_PAGE = 50

const WATERMARK_TOOLTIP =
  'If a collector has not run in about a day, the next run collects the last 24 hours instead of resuming here.'

// Same text as RESET_NOTE in api/routers/collectors.py, shown before starting over so the choice is
// informed; the note in the response is shown after.
const RESET_NOTE_STATIC =
  'The next run collects from the point chosen here minus a 30 minute overlap, but never further back than ' +
  '7 days. A point newer than about 24 hours behaves the same as starting from scratch: the run collects ' +
  'the last 24 hours.'

// `silent` refreshes in place: after an action the list must not collapse into the spinner.
async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = null
  try {
    collectors.value = await api.listCollectors()
  } catch (error) {
    loadError.value = errorText(error)
  } finally {
    loading.value = false
  }
}

async function onToggle(c: CollectorState, value: boolean) {
  toggling.value = true
  toggleErrors[c.source] = undefined
  const previous = c.active
  c.active = value
  try {
    const config = await api.getConfig()
    const saved = await api.updateConfig({
      ...config,
      active_sources: { ...config.active_sources, [c.source]: value },
    })
    c.active = saved.active_sources[c.source] ?? value
  } catch (error) {
    c.active = previous
    const message = errorText(error)
    toggleErrors[c.source] = message
    $q.notify({ type: 'negative', message: `Could not change ${humanize(c.source)}: ${message}` })
  } finally {
    toggling.value = false
  }
}

// --- run one source ------------------------------------------------------------------------------

// A finished run is recognised by last_attempt.at moving: the run itself is a background task with no
// id, so the collector's own cursor is the only thing to watch. One interval serves every source.
const runWatch = new Map<string, { baseline: string | null; startedAt: number }>()
let runTimer: ReturnType<typeof setInterval> | null = null
let runPolling = false
let runPollFailures = 0

function stopRunTimer() {
  if (runTimer !== null) clearInterval(runTimer)
  runTimer = null
}

function endWatch(source: string) {
  runWatch.delete(source)
  running[source] = false
  if (!runWatch.size) stopRunTimer()
}

async function pollRuns() {
  if (runPolling) return
  runPolling = true
  try {
    const list = await api.listCollectors()
    runPollFailures = 0
    // A toggle in flight holds a reference to the row it is editing, so the list is not swapped under it.
    if (!toggling.value) collectors.value = list
    for (const [source, watch] of [...runWatch]) {
      const attempt = list.find((state) => state.source === source)?.last_attempt ?? null
      if ((attempt?.at ?? null) !== watch.baseline) {
        endWatch(source)
        $q.notify({
          type: attempt?.status === 'ok' || attempt?.status === 'success' ? 'positive' : 'warning',
          message: `${humanize(source)}: run finished (${attempt?.status ?? 'unknown'})`,
        })
      } else if (Date.now() - watch.startedAt > RUN_TIMEOUT_MS) {
        endWatch(source)
        $q.notify({ type: 'warning', message: `${humanize(source)}: still running; reload to see the result later` })
      }
    }
  } catch {
    runPollFailures += 1
    if (runPollFailures >= RUN_MAX_POLL_FAILURES) {
      for (const source of [...runWatch.keys()]) endWatch(source)
      $q.notify({ type: 'negative', message: 'Lost contact with the API while a run was in progress' })
    }
  } finally {
    runPolling = false
  }
}

async function onRun(c: CollectorState) {
  if (running[c.source]) return
  running[c.source] = true
  try {
    await api.runCollector(c.source)
  } catch (error) {
    running[c.source] = false
    $q.notify({
      type: 'negative',
      message: error instanceof ApiError && error.status === 409
        ? 'A run is already in progress'
        : `Could not start ${humanize(c.source)}: ${errorText(error)}`,
    })
    return
  }
  $q.notify({ type: 'positive', message: `Started ${humanize(c.source)}` })
  runWatch.set(c.source, { baseline: c.last_attempt?.at ?? null, startedAt: Date.now() })
  if (runTimer === null) {
    runPollFailures = 0
    runTimer = setInterval(() => void pollRuns(), RUN_POLL_MS)
  }
}

/** Confirms before starting a run: collecting writes data, so a stray click should not trigger it. */
function confirmRun(c: CollectorState) {
  $q.dialog({
    title: 'Run now?',
    message: `Collect from ${humanize(c.source)} now?`,
    persistent: true,
    cancel: { label: 'Cancel', flat: true, noCaps: true },
    ok: { label: 'Run now', color: 'primary', noCaps: true },
  }).onOk(() => {
    void onRun(c)
  })
}

// --- dry run -------------------------------------------------------------------------------------

type DryStage = 'form' | 'running' | 'done' | 'failed'

const dryOpen = ref(false)
const drySource = ref<string | null>(null)
const dryStage = ref<DryStage>('form')
const dryHours = ref<number | string>(24)
const dryLimit = ref<number | string>(5)
const dryResult = ref<DryRunResult | null>(null)
const dryError = ref<string | null>(null)
const showRaw = ref(false)

let drySession = 0
let dryTimer: ReturnType<typeof setInterval> | null = null
let dryPolling = false

const dryValid = computed(() => {
  const hours = Number(dryHours.value)
  const limit = Number(dryLimit.value)
  return (
    dryHours.value !== '' && dryLimit.value !== '' &&
    Number.isInteger(hours) && hours >= 1 && hours <= DRY_MAX_HOURS &&
    Number.isInteger(limit) && limit >= 0 && limit <= DRY_MAX_LIMIT
  )
})

// The API has no field for raw payloads today; when a result carries one it is shown as text.
const dryRaw = computed<string | null>(() => {
  const raw = (dryResult.value as (DryRunResult & { raw?: unknown }) | null)?.raw
  if (raw === undefined || raw === null) return null
  return typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2)
})

function stopDryTimer() {
  if (dryTimer !== null) clearInterval(dryTimer)
  dryTimer = null
  dryPolling = false
}

function resetDry() {
  drySession += 1
  stopDryTimer()
  dryStage.value = 'form'
  dryResult.value = null
  dryError.value = null
  showRaw.value = false
}

function openDryRun(c: CollectorState) {
  resetDry()
  dryHours.value = 24
  dryLimit.value = 5
  drySource.value = c.source
  dryOpen.value = true
}

function closeDryRun() {
  resetDry()
  dryOpen.value = false
  drySource.value = null
}

function dryAgain() {
  resetDry()
}

async function pollDryRun(jobId: string, session: number) {
  if (dryPolling) return
  dryPolling = true
  try {
    const job = await api.getDryRun(jobId)
    if (session !== drySession) return
    if (job.status === 'running') return
    stopDryTimer()
    if (job.status === 'done' && job.result) {
      dryResult.value = job.result
      dryStage.value = 'done'
    } else {
      dryError.value = 'The dry run failed. Check the connection and try again.'
      dryStage.value = 'failed'
    }
  } catch (error) {
    if (session !== drySession) return
    stopDryTimer()
    dryError.value = error instanceof ApiError && error.status === 404
      ? 'This dry run expired. Start it again.'
      : errorText(error)
    dryStage.value = 'failed'
  } finally {
    if (session === drySession) dryPolling = false
  }
}

async function onStartDryRun() {
  const source = drySource.value
  if (!source || !dryValid.value) return
  const session = ++drySession
  dryError.value = null
  dryStage.value = 'running'
  try {
    const { job_id: jobId } = await api.startDryRun(source, { hours: Number(dryHours.value), limit: Number(dryLimit.value) })
    if (session !== drySession) return
    dryTimer = setInterval(() => void pollDryRun(jobId, session), DRY_POLL_MS)
  } catch (error) {
    if (session !== drySession) return
    dryStage.value = 'form'
    dryError.value = error instanceof ApiError && error.status === 429
      ? 'Too many dry runs are in progress; try again shortly'
      : errorText(error)
  }
}

// --- start over (reset watermark) -----------------------------------------------------------------

const RESET_OPTIONS: Array<{ key: string; value: number | null; label: string }> = [
  ...[1, 2, 3, 4, 5, 6, 7].map((days) => ({
    key: String(days),
    value: days,
    label: days === 1 ? '1 day back' : `${days} days back`,
  })),
  { key: 'none', value: null, label: 'From scratch' },
]

const resetSource = ref<string | null>(null)
const resetChoice = ref<number | null>(1)
const resetting = ref(false)
const resetError = ref<string | null>(null)
const resetOutcome = ref<ResetResult | null>(null)

function openReset(c: CollectorState) {
  resetChoice.value = 1
  resetError.value = null
  resetOutcome.value = null
  resetSource.value = c.source
}

function closeReset() {
  if (resetting.value) return
  resetSource.value = null
}

async function onReset() {
  const source = resetSource.value
  if (!source) return
  resetting.value = true
  resetError.value = null
  try {
    resetOutcome.value = await api.resetCollector(source, resetChoice.value ?? undefined)
    $q.notify({ type: 'positive', message: `${humanize(source)} will start over` })
    await load(true)
  } catch (error) {
    resetError.value = errorText(error)
  } finally {
    resetting.value = false
  }
}

// --- clear failures ------------------------------------------------------------------------------

async function onClear(c: CollectorState) {
  try {
    await api.clearFailures(c.source)
    $q.notify({ type: 'positive', message: `Cleared the failures of ${humanize(c.source)}` })
    await load(true)
  } catch (error) {
    $q.notify({ type: 'negative', message: `Could not clear ${humanize(c.source)}: ${errorText(error)}` })
  }
}

/** Confirms before clearing: it discards the record of what failed. */
function confirmClear(c: CollectorState) {
  $q.dialog({
    title: 'Clear failures?',
    message: `Clear the failure count for ${humanize(c.source)}? SignalSlate will try again as if nothing failed.`,
    persistent: true,
    cancel: { label: 'Cancel', flat: true, noCaps: true },
    ok: { label: 'Clear failures', color: 'negative', noCaps: true },
  }).onOk(() => {
    void onClear(c)
  })
}

// --- items ---------------------------------------------------------------------------------------

const itemsSource = ref<string | null>(null)
const items = ref<ItemRow[]>([])
const itemsTotal = ref(0)
const nextBeforeId = ref<number | null>(null)
const itemsLoading = ref(false)
const itemsMoreLoading = ref(false)
const itemsError = ref<string | null>(null)
const filterText = ref<string | null>('')
const appliedType = ref<string | undefined>(undefined)

const detailRow = ref<ItemRow | null>(null)
const detail = ref<ItemDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref<string | null>(null)

// Bumped on every reset of the browser, so a response for a source, filter or item that is no longer
// on screen is dropped.
let itemsSession = 0
let detailSession = 0

async function loadItems(first: boolean) {
  const source = itemsSource.value
  if (!source) return
  const session = itemsSession
  itemsError.value = null
  if (first) {
    items.value = []
    nextBeforeId.value = null
    itemsLoading.value = true
  } else {
    itemsMoreLoading.value = true
  }
  try {
    const page = await api.listItems(source, {
      limit: ITEMS_PAGE,
      beforeId: first ? undefined : (nextBeforeId.value ?? undefined),
      itemType: appliedType.value,
    })
    if (session !== itemsSession) return
    items.value = first ? page.items : [...items.value, ...page.items]
    nextBeforeId.value = page.next_before_id
    itemsTotal.value = page.total
  } catch (error) {
    if (session !== itemsSession) return
    itemsError.value = errorText(error)
  } finally {
    if (session === itemsSession) {
      itemsLoading.value = false
      itemsMoreLoading.value = false
    }
  }
}

function openItems(c: CollectorState) {
  itemsSession += 1
  detailSession += 1
  filterText.value = ''
  appliedType.value = undefined
  detailRow.value = null
  detail.value = null
  itemsSource.value = c.source
  void loadItems(true)
}

function closeItems() {
  itemsSession += 1
  detailSession += 1
  itemsSource.value = null
  detailRow.value = null
  detail.value = null
  items.value = []
}

function applyFilter() {
  itemsSession += 1
  appliedType.value = filterText.value?.trim() || undefined
  void loadItems(true)
}

async function openItem(row: ItemRow) {
  const source = itemsSource.value
  if (!source) return
  const session = ++detailSession
  detailRow.value = row
  detail.value = null
  detailError.value = null
  detailLoading.value = true
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

onMounted(() => load())

onBeforeUnmount(() => {
  stopRunTimer()
  runWatch.clear()
  drySession += 1
  stopDryTimer()
  itemsSession += 1
  detailSession += 1
})
</script>
