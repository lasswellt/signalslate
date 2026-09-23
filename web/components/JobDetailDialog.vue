<template>
  <DialogShell
    :model-value="open"
    :title="dialogTitle"
    :subtitle="dialogSubtitle"
    width="640px"
    @update:model-value="onModelUpdate"
    @close="onDialogClose"
  >
    <div data-testid="job-detail-dialog">
      <template v-if="posting">
        <q-banner v-if="isClosed" dense class="bg-grey-3 q-mb-md" data-testid="job-closed-banner">
          This posting has closed.
        </q-banner>

        <div class="row items-center q-gutter-sm q-mb-md" data-testid="job-fit">
          <div data-testid="job-fit-score">
            <StatusChip kind="fit" :value="posting.fit_score" />
          </div>
          <span class="text-grey-8" data-testid="job-fit-reason">{{ posting.fit_reason ?? '—' }}</span>
        </div>

        <div class="q-gutter-xs q-mb-md" data-testid="job-details">
          <div>Job board: {{ humanize(posting.ats_kind) }}</div>
          <div>Compensation: {{ posting.comp_text ?? '—' }}</div>
          <div>Remote: {{ posting.remote === null ? '—' : posting.remote ? 'Yes' : 'No' }}</div>
          <div>First seen: {{ formatDate(posting.first_seen) }} · {{ relativeTime(posting.first_seen) }}</div>
        </div>

        <q-banner v-if="createdApplication" dense class="bg-positive text-white q-mb-md" data-testid="job-application-created">
          Application created.
        </q-banner>

        <q-banner v-if="submitError" dense class="bg-negative text-white" data-testid="job-start-application-error">
          {{ submitError }}
        </q-banner>
      </template>
    </div>

    <template #actions>
      <q-btn
        v-if="posting"
        flat
        no-caps
        label="Open posting"
        :href="posting.apply_url"
        target="_blank"
        rel="noopener noreferrer"
        data-testid="job-apply-link"
      />
      <q-btn
        v-if="posting && !createdApplication"
        unelevated
        no-caps
        color="primary"
        :loading="submitting"
        :disable="submitting"
        label="Start application"
        data-testid="job-start-application"
        @click="startApplication"
      />
      <q-btn flat no-caps label="Close" data-testid="job-detail-close" @click="close" />
    </template>
  </DialogShell>
</template>

<script setup lang="ts">
import { useJobsApi } from '~/composables/useJobsApi'
import type { ApplicationOut, PostingOut } from '~/composables/useJobsApi'
import DialogShell from '~/components/ui/DialogShell.vue'
import StatusChip from '~/components/ui/StatusChip.vue'

const props = defineProps<{
  open: boolean
  posting: PostingOut | null
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  closed: []
  applicationCreated: [application: ApplicationOut]
}>()

const api = useJobsApi()

const submitting = ref(false)
const submitError = ref<string | null>(null)
const createdApplication = ref<ApplicationOut | null>(null)

const isClosed = computed(() => props.posting?.closed_at != null)

const dialogTitle = computed(() => props.posting?.title ?? 'Job posting')
const dialogSubtitle = computed(() => {
  const posting = props.posting
  if (!posting) return undefined
  return `${posting.company_name || '—'} · ${posting.location || '—'}`
})

/**
 * Creates an application for the currently displayed posting via POST /jobs/applications.
 * @returns Nothing; on success stores the created application and emits `applicationCreated`,
 *   on failure surfaces the message in `submitError`.
 */
async function startApplication() {
  if (!props.posting) return
  submitting.value = true
  submitError.value = null
  try {
    const application = await api.createApplication({ posting_id: props.posting.id })
    createdApplication.value = application
    emit('applicationCreated', application)
  } catch (err) {
    submitError.value = errorText(err)
  } finally {
    submitting.value = false
  }
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
      submitError.value = null
      createdApplication.value = null
    }
  },
)
</script>
