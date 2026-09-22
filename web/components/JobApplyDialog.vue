<template>
  <!-- Not persistent: a click outside or Esc is just Close, same as the Close button; nothing here
       survives a reopen (reset() runs every time the dialog opens, mirroring DomainPurchaseDialog.vue). -->
  <q-dialog :model-value="open" @update:model-value="onModelUpdate">
    <q-card style="min-width: 360px; max-width: 640px; width: 100%" data-testid="job-apply-dialog">
      <q-card-section>
        <div class="text-h6" data-testid="job-apply-title">Apply</div>
      </q-card-section>

      <q-card-section v-if="loading" data-testid="job-packet-loading">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="80%" />
        <q-skeleton type="text" width="40%" />
      </q-card-section>

      <q-card-section v-else-if="loadError" data-testid="job-packet-load-error">
        <q-banner dense class="bg-negative text-white">{{ loadError }}</q-banner>
        <q-btn flat no-caps label="Retry" data-testid="job-packet-retry" @click="prepare" />
      </q-card-section>

      <template v-else-if="application">
        <q-card-section class="q-gutter-sm" data-testid="job-packet">
          <q-input
            v-model="coverLetterText"
            type="textarea"
            label="Cover letter"
            outlined
            autogrow
            :disable="saving"
            data-testid="job-cover-letter"
          />

          <div data-testid="job-screening-drafts">
            <div class="text-subtitle2">Screening questions</div>
            <div v-for="(draft, index) in screeningDrafts" :key="index" class="q-mb-sm" data-testid="job-screening-draft">
              <div class="text-weight-medium">{{ draft.question }}</div>
              <div>{{ draft.answer }}</div>
            </div>
          </div>

          <q-banner v-if="saveError" dense class="bg-negative text-white" data-testid="job-packet-save-error">
            {{ saveError }}
          </q-banner>
          <div v-if="justSaved" class="text-positive" data-testid="job-packet-saved">Saved</div>

          <q-btn
            flat
            no-caps
            color="primary"
            label="Save"
            :loading="saving"
            :disable="saving"
            data-testid="job-packet-save"
            @click="savePacket"
          />
        </q-card-section>

        <q-separator />

        <q-card-section class="q-gutter-sm" data-testid="job-assist">
          <q-banner v-if="startError" dense class="bg-negative text-white" data-testid="job-start-assist-error">
            {{ startError }}
          </q-banner>

          <div data-testid="job-assist-state">Assist status: {{ application.assist_state }}</div>

          <div v-if="showQueuedHint" class="text-grey-8" data-testid="job-assist-queued-hint">
            Run <code>python -m pipeline.jobs.assist --watch</code> on your desktop to pick this up.
          </div>

          <q-btn
            flat
            no-caps
            color="primary"
            label="Start assist"
            :loading="starting"
            :disable="starting || isAssistActive"
            data-testid="job-start-assist"
            @click="startAssist"
          />
        </q-card-section>

        <q-separator />

        <q-card-section class="q-gutter-sm" data-testid="job-submit">
          <q-banner v-if="submitError" dense class="bg-negative text-white" data-testid="job-mark-submitted-error">
            {{ submitError }}
          </q-banner>

          <div v-if="confirmingSubmit" class="q-gutter-sm" data-testid="job-confirm-submitted-banner">
            <q-banner dense class="bg-warning text-black">Mark this application as submitted? This cannot be undone.</q-banner>
            <q-btn flat no-caps label="Cancel" :disable="submitting" data-testid="job-cancel-submitted" @click="confirmingSubmit = false" />
            <q-btn
              color="negative"
              no-caps
              label="Confirm submitted"
              :loading="submitting"
              data-testid="job-confirm-submitted"
              @click="confirmMarkSubmitted"
            />
          </div>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat no-caps label="Close" data-testid="job-apply-close" @click="close" />
          <q-btn
            color="primary"
            no-caps
            label="Mark submitted"
            :disable="submitting || !canMarkSubmitted"
            data-testid="job-mark-submitted"
            @click="onMarkSubmittedClick"
          />
        </q-card-actions>
      </template>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { ApiError } from '~/composables/useApi'
import { useJobsApi } from '~/composables/useJobsApi'
import type { ApplicationOut } from '~/composables/useJobsApi'

// How often the running assist session's state is polled once queued (mirrors
// DomainPurchaseDialog.vue's setInterval/onMounted/onUnmounted pattern, reused here for polling
// instead of a countdown).
const ASSIST_POLL_INTERVAL_MS = 3000
// If assist_state is still "queued" this long after queuing, the desktop runner probably isn't
// watching the queue yet — point the user at the command that picks it up.
const ASSIST_QUEUED_HINT_MS = 15000

interface ScreeningDraft {
  question: string
  answer: string
}

const props = defineProps<{
  open: boolean
  applicationId: number
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  closed: []
  submitted: [application: ApplicationOut]
}>()

const api = useJobsApi()

const loading = ref(false)
const loadError = ref<string | null>(null)
const application = ref<ApplicationOut | null>(null)

const coverLetterText = ref('')
const screeningDrafts = ref<ScreeningDraft[]>([])
const saving = ref(false)
const saveError = ref<string | null>(null)
const justSaved = ref(false)

const starting = ref(false)
const startError = ref<string | null>(null)
const queuedSinceMs = ref<number | null>(null)
const nowMs = ref(Date.now())
let pollTimer: ReturnType<typeof setInterval> | null = null

const confirmingSubmit = ref(false)
const submitting = ref(false)
const submitError = ref<string | null>(null)

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** Pulls the editable fields out of an ApplicationOut's loosely-typed `packet` JSON
 * (pipeline/jobs/packet.py's {cover_letter_text, screening_drafts} shape). */
function parsePacket(raw: Record<string, unknown> | null): { coverLetterText: string; screeningDrafts: ScreeningDraft[] } {
  if (!raw) return { coverLetterText: '', screeningDrafts: [] }
  const coverLetterText = typeof raw.cover_letter_text === 'string' ? raw.cover_letter_text : ''
  const screeningDrafts = Array.isArray(raw.screening_drafts)
    ? raw.screening_drafts.filter(isRecord).map((draft) => ({
        question: typeof draft.question === 'string' ? draft.question : '',
        answer: typeof draft.answer === 'string' ? draft.answer : '',
      }))
    : []
  return { coverLetterText, screeningDrafts }
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong'
}

const isAssistActive = computed(() => {
  const state = application.value?.assist_state
  return state === 'queued' || state === 'claimed' || state === 'running'
})

const showQueuedHint = computed(() => {
  if (application.value?.assist_state !== 'queued' || queuedSinceMs.value === null) return false
  return nowMs.value - queuedSinceMs.value >= ASSIST_QUEUED_HINT_MS
})

const canMarkSubmitted = computed(() => !!application.value?.packet)

function stopPolling() {
  if (pollTimer !== null) clearInterval(pollTimer)
  pollTimer = null
}

/** One tick of the assist-state poll: re-lists applications (there is no GET-by-id route; see
 * api/routers/job_apply.py) and updates from the matching row. Stops itself once the assist
 * session reaches a terminal state. Poll failures surface via startError rather than throwing, so
 * one bad tick doesn't tear down the interval. */
async function pollAssistState() {
  const current = application.value
  if (!current) return
  try {
    const apps = await api.listApplications()
    const updated = apps.find((item) => item.id === current.id)
    nowMs.value = Date.now()
    if (!updated) return
    application.value = updated
    if (updated.assist_state !== 'queued') queuedSinceMs.value = null
    else if (queuedSinceMs.value === null) queuedSinceMs.value = nowMs.value
    if (updated.assist_state === 'done' || updated.assist_state === 'failed') stopPolling()
  } catch (err) {
    nowMs.value = Date.now()
    startError.value = errorMessage(err)
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => void pollAssistState(), ASSIST_POLL_INTERVAL_MS)
}

/**
 * Drafts (or re-drafts) the application's cover letter + screening-answer packet.
 * @returns Nothing; on success stores the application/packet, on failure sets loadError.
 */
async function prepare() {
  loading.value = true
  loadError.value = null
  try {
    const result = await api.prepareApplicationPacket(props.applicationId)
    application.value = result
    const parsed = parsePacket(result.packet)
    coverLetterText.value = parsed.coverLetterText
    screeningDrafts.value = parsed.screeningDrafts
  } catch (err) {
    loadError.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

/**
 * Saves the edited cover letter text, which re-renders the PDF server-side.
 * @returns Nothing; on success shows a transient "Saved" indicator, on failure sets saveError.
 */
async function savePacket() {
  if (!application.value) return
  saving.value = true
  saveError.value = null
  try {
    const result = await api.editApplicationPacket(application.value.id, { cover_letter_text: coverLetterText.value })
    application.value = result
    justSaved.value = true
  } catch (err) {
    saveError.value = errorMessage(err)
  } finally {
    saving.value = false
  }
}

/**
 * Queues an apply-assist session for this application and starts polling its state.
 * @returns Nothing; on success starts polling, on failure sets startError.
 */
async function startAssist() {
  if (!application.value || isAssistActive.value) return
  starting.value = true
  startError.value = null
  try {
    const result = await api.queueAssist(application.value.id)
    application.value = result
    nowMs.value = Date.now()
    queuedSinceMs.value = result.assist_state === 'queued' ? nowMs.value : null
    startPolling()
  } catch (err) {
    startError.value = errorMessage(err)
  } finally {
    starting.value = false
  }
}

function onMarkSubmittedClick() {
  submitError.value = null
  confirmingSubmit.value = true
}

/**
 * Moves the application to "submitted" — the one irreversible, user-only action per
 * pipeline/jobs/apply.py's own contract — after the confirm step above.
 * @returns Nothing; on success emits `submitted`, on failure (e.g. 422 illegal_transition) sets
 *   submitError and leaves the confirm step open.
 */
async function confirmMarkSubmitted() {
  if (!application.value) return
  submitting.value = true
  submitError.value = null
  try {
    const result = await api.updateApplicationStatus(application.value.id, { status: 'submitted' })
    application.value = result
    confirmingSubmit.value = false
    emit('submitted', result)
  } catch (err) {
    submitError.value = errorMessage(err)
  } finally {
    submitting.value = false
  }
}

watch(coverLetterText, () => {
  justSaved.value = false
})

function reset() {
  loading.value = false
  loadError.value = null
  application.value = null
  coverLetterText.value = ''
  screeningDrafts.value = []
  saving.value = false
  saveError.value = null
  justSaved.value = false
  starting.value = false
  startError.value = null
  stopPolling()
  queuedSinceMs.value = null
  nowMs.value = Date.now()
  confirmingSubmit.value = false
  submitting.value = false
  submitError.value = null
}

function close() {
  emit('update:open', false)
  emit('closed')
}

function onModelUpdate(value: boolean) {
  emit('update:open', value)
  if (!value) emit('closed')
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      reset()
      void prepare()
    } else {
      stopPolling()
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  stopPolling()
})
</script>
