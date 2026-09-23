<template>
  <DialogShell
    :model-value="open"
    :title="dialogTitle"
    :subtitle="dialogSubtitle"
    width="640px"
    :busy="saving"
    :dirty="isDirty"
    @update:model-value="onModelUpdate"
    @close="onDialogClose"
  >
    <div data-testid="job-apply-dialog">
      <div v-if="loading" data-testid="job-packet-loading">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="80%" />
        <q-skeleton type="text" width="40%" />
      </div>

      <div v-else-if="loadError" data-testid="job-packet-load-error">
        <q-banner dense class="bg-negative text-white">{{ loadError }}</q-banner>
        <q-btn flat no-caps color="primary" label="Retry" data-testid="job-packet-retry" @click="prepare" />
      </div>

      <template v-else-if="application">
        <div class="q-mb-md" data-testid="job-packet">
          <div class="text-subtitle2">Cover letter</div>
          <q-input
            v-model="coverLetterText"
            type="textarea"
            outlined
            autogrow
            :disable="saving"
            data-testid="job-cover-letter"
          />
          <div class="row items-center q-gutter-sm q-mt-xs">
            <q-btn
              unelevated
              no-caps
              color="primary"
              label="Save"
              :loading="saving"
              :disable="saving"
              data-testid="job-packet-save"
              @click="savePacket"
            />
            <span class="text-caption text-grey-7" data-testid="job-cover-letter-count">{{ coverLetterText.length }} characters</span>
          </div>
          <q-banner v-if="saveError" dense class="bg-negative text-white q-mt-sm" data-testid="job-packet-save-error">
            {{ saveError }}
          </q-banner>
          <div v-if="justSaved" class="text-positive q-mt-xs" data-testid="job-packet-saved">Saved</div>
        </div>

        <q-separator />

        <div class="q-my-md" data-testid="job-screening-drafts">
          <div class="text-subtitle2">Screening answers</div>
          <div class="text-caption text-grey-7 q-mb-sm">Draft — edit in the assistant</div>
          <div v-for="(draft, index) in screeningDrafts" :key="index" class="q-mb-sm" data-testid="job-screening-draft">
            <div class="text-weight-medium">{{ draft.question }}</div>
            <div>{{ draft.answer }}</div>
          </div>
        </div>

        <q-separator />

        <div class="q-my-md" data-testid="job-documents">
          <div class="text-subtitle2">Documents</div>
          <div v-if="!application.cover_letter_path && !application.resume_path" class="text-grey-8">
            No documents yet.
          </div>
          <div v-else class="row q-gutter-sm">
            <q-btn
              v-if="application.cover_letter_path"
              flat
              dense
              no-caps
              color="primary"
              icon="description"
              label="Cover letter (PDF)"
              :href="api.applicationFileUrl(application.id, 'cover_letter')"
              target="_blank"
              rel="noopener noreferrer"
              data-testid="job-cover-letter-link"
            />
            <q-btn
              v-if="application.resume_path"
              flat
              dense
              no-caps
              color="primary"
              icon="description"
              label="Resume (PDF)"
              :href="api.applicationFileUrl(application.id, 'resume')"
              target="_blank"
              rel="noopener noreferrer"
              data-testid="job-resume-link"
            />
          </div>
        </div>

        <q-separator />

        <div class="q-mt-md" data-testid="job-assist">
          <div class="text-subtitle2">Guided apply</div>
          <div class="row items-center q-gutter-sm q-my-xs" data-testid="job-assist-state">
            <StatusChip kind="assist" :value="application.assist_state" />
          </div>
          <div class="text-grey-8" data-testid="job-assist-explanation">{{ assistExplanation }}</div>

          <q-banner v-if="startError" dense class="bg-negative text-white q-mt-sm" data-testid="job-start-assist-error">
            {{ startError }}
          </q-banner>

          <div v-if="showQueuedHint" class="text-grey-8 q-mt-xs" data-testid="job-assist-queued-hint">
            The assistant runs on your computer — start it from the desktop helper.
          </div>

          <q-btn
            unelevated
            no-caps
            color="primary"
            label="Start assist"
            class="q-mt-sm"
            :loading="starting"
            :disable="starting || isAssistActive"
            data-testid="job-start-assist"
            @click="startAssist"
          />

          <q-expansion-item dense label="How to start the assistant" class="q-mt-sm" data-testid="job-assist-how-to-start">
            <q-card-section class="text-body2">
              Run this on the computer with your browser session:
              <div><code data-testid="job-assist-command">python -m pipeline.jobs.assist --watch</code></div>
            </q-card-section>
          </q-expansion-item>
        </div>

        <q-banner v-if="submitError" dense class="bg-negative text-white q-mt-md" data-testid="job-mark-submitted-error">
          {{ submitError }}
        </q-banner>
      </template>
    </div>

    <template #actions>
      <q-btn flat no-caps label="Close" data-testid="job-apply-close" @click="close" />
      <q-btn
        v-if="application"
        unelevated
        color="primary"
        no-caps
        label="Mark as submitted"
        :disable="submitting || !canMarkSubmitted"
        :loading="submitting"
        data-testid="job-mark-submitted"
        @click="onMarkSubmittedClick"
      />
    </template>
  </DialogShell>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { useJobsApi } from '~/composables/useJobsApi'
import type { ApplicationOut } from '~/composables/useJobsApi'
import DialogShell from '~/components/ui/DialogShell.vue'
import StatusChip from '~/components/ui/StatusChip.vue'

// How often the running assist session's state is polled once queued (mirrors
// DomainPurchaseDialog.vue's setInterval/onMounted/onUnmounted pattern, reused here for polling
// instead of a countdown).
const ASSIST_POLL_INTERVAL_MS = 3000
// If assist_state is still "queued" this long after queuing, the desktop runner probably isn't
// watching the queue yet — point the user at the desktop helper.
const ASSIST_QUEUED_HINT_MS = 15000

// Plain-language explanation for each of api/routers/job_apply.py's _ASSIST_STATES.
const ASSIST_EXPLANATIONS: Record<string, string> = {
  idle: 'Guided apply has not started for this application.',
  queued: 'Waiting for the desktop assistant to pick this up.',
  claimed: 'The desktop assistant has claimed this application.',
  running: 'The desktop assistant is filling out this application now.',
  paused: 'The desktop assistant paused and needs your input.',
  done: 'The desktop assistant finished filling out this application.',
  failed: 'The desktop assistant hit an error working on this application.',
}

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
const $q = useQuasar()

const loading = ref(false)
const loadError = ref<string | null>(null)
const application = ref<ApplicationOut | null>(null)

const coverLetterText = ref('')
const baselineCoverLetterText = ref('')
const screeningDrafts = ref<ScreeningDraft[]>([])
const saving = ref(false)
const saveError = ref<string | null>(null)
const justSaved = ref(false)

const starting = ref(false)
const startError = ref<string | null>(null)
const queuedSinceMs = ref<number | null>(null)
const nowMs = ref(Date.now())
let pollTimer: ReturnType<typeof setInterval> | null = null

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

const dialogTitle = computed(() => {
  const app = application.value
  if (!app) return 'Apply'
  return app.posting_title ? `Apply: ${app.posting_title}` : `Application #${app.id}`
})

const dialogSubtitle = computed(() => application.value?.company_name ?? undefined)

const isDirty = computed(() => coverLetterText.value !== baselineCoverLetterText.value)

const isAssistActive = computed(() => {
  const state = application.value?.assist_state
  return state === 'queued' || state === 'claimed' || state === 'running'
})

const assistExplanation = computed(() => {
  const state = application.value?.assist_state
  if (!state) return ASSIST_EXPLANATIONS.idle
  return ASSIST_EXPLANATIONS[state] ?? humanize(state)
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
    startError.value = errorText(err)
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
    baselineCoverLetterText.value = parsed.coverLetterText
    screeningDrafts.value = parsed.screeningDrafts
  } catch (err) {
    loadError.value = errorText(err)
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
    baselineCoverLetterText.value = coverLetterText.value
    justSaved.value = true
  } catch (err) {
    saveError.value = errorText(err)
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
    startError.value = errorText(err)
  } finally {
    starting.value = false
  }
}

/**
 * Moves the application to "submitted" — the one irreversible, user-only action per
 * pipeline/jobs/apply.py's own contract — after the confirm dialog below.
 * @returns Nothing; on success emits `submitted`, on failure (e.g. 422 illegal_transition) sets
 *   submitError.
 */
async function confirmMarkSubmitted() {
  if (!application.value) return
  submitting.value = true
  submitError.value = null
  try {
    const result = await api.updateApplicationStatus(application.value.id, { status: 'submitted' })
    application.value = result
    emit('submitted', result)
  } catch (err) {
    submitError.value = errorText(err)
  } finally {
    submitting.value = false
  }
}

function onMarkSubmittedClick() {
  submitError.value = null
  $q.dialog({
    title: 'Mark as submitted?',
    message: 'This records the application as submitted. It cannot be undone.',
    cancel: { label: 'Cancel', flat: true, noCaps: true, 'data-testid': 'job-cancel-submitted' },
    ok: { label: 'Mark as submitted', color: 'primary', unelevated: true, noCaps: true, 'data-testid': 'job-confirm-submitted' },
    persistent: true,
  }).onOk(() => {
    void confirmMarkSubmitted()
  })
}

watch(coverLetterText, () => {
  justSaved.value = false
})

function reset() {
  loading.value = false
  loadError.value = null
  application.value = null
  coverLetterText.value = ''
  baselineCoverLetterText.value = ''
  screeningDrafts.value = []
  saving.value = false
  saveError.value = null
  justSaved.value = false
  starting.value = false
  startError.value = null
  stopPolling()
  queuedSinceMs.value = null
  nowMs.value = Date.now()
  submitting.value = false
  submitError.value = null
}

function close() {
  emit('update:open', false)
  emit('closed')
}

function onModelUpdate(value: boolean) {
  emit('update:open', value)
}

function onDialogClose() {
  emit('closed')
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
