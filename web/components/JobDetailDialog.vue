<template>
  <!-- Not persistent: read-only detail with one action (start application), so a click outside or
       Esc closes it the same as the Close button. -->
  <q-dialog :model-value="open" @update:model-value="onModelUpdate">
    <q-card style="min-width: 360px; max-width: 720px; width: 100%" data-testid="job-detail-dialog">
      <template v-if="posting">
        <q-card-section>
          <div class="text-h6" data-testid="job-title">{{ posting.title }}</div>
          <div class="text-subtitle2 text-grey-8" data-testid="job-company">{{ posting.company_name }}</div>
        </q-card-section>

        <q-card-section v-if="isClosed" data-testid="job-closed-banner">
          <q-banner dense class="bg-grey-3">This posting has closed.</q-banner>
        </q-card-section>

        <q-card-section data-testid="job-fit">
          <div class="row items-center q-gutter-sm">
            <q-badge :color="fitColor" data-testid="job-fit-score">
              {{ posting.fit_score ?? 'Unscored' }}
            </q-badge>
          </div>
          <div class="text-grey-8" data-testid="job-fit-reason">
            {{ posting.fit_reason ?? 'No fit reason available.' }}
          </div>
        </q-card-section>

        <q-card-section data-testid="job-details">
          <div>Location: {{ posting.location ?? 'Unknown' }}</div>
          <div>Remote: {{ posting.remote === null ? 'Unknown' : posting.remote ? 'Yes' : 'No' }}</div>
          <div v-if="posting.comp_text">Compensation: {{ posting.comp_text }}</div>
          <div>ATS: {{ posting.ats_kind }}</div>
          <div>First seen: {{ formatDate(posting.first_seen) }}</div>
          <div>Last seen: {{ formatDate(posting.last_seen) }}</div>
        </q-card-section>

        <q-card-section data-testid="job-apply-link">
          <q-btn
            flat
            no-caps
            color="primary"
            label="View posting"
            :href="posting.apply_url"
            target="_blank"
            rel="noopener noreferrer"
          />
        </q-card-section>

        <q-card-section v-if="createdApplication" data-testid="job-application-created">
          <q-banner dense class="bg-positive text-white">Application created.</q-banner>
        </q-card-section>

        <q-card-section v-if="submitError" data-testid="job-start-application-error">
          <q-banner dense class="bg-negative text-white">{{ submitError }}</q-banner>
        </q-card-section>
      </template>

      <q-card-actions align="right">
        <q-btn
          v-if="posting && !createdApplication"
          flat
          no-caps
          color="primary"
          :loading="submitting"
          :disable="submitting"
          label="Start application"
          data-testid="job-start-application"
          @click="startApplication"
        />
        <q-btn flat no-caps label="Close" data-testid="job-detail-close" @click="close" />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { ApiError, parseUtc } from '~/composables/useApi'
import { useJobsApi } from '~/composables/useJobsApi'
import type { ApplicationOut, PostingOut } from '~/composables/useJobsApi'

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

type FitTier = 'high' | 'med' | 'low'

const fitTier = computed<FitTier | null>(() => {
  const score = props.posting?.fit_score
  if (score === null || score === undefined) return null
  if (score >= 75) return 'high'
  if (score >= 50) return 'med'
  return 'low'
})

const fitColor = computed(() => {
  switch (fitTier.value) {
    case 'high':
      return 'positive'
    case 'med':
      return 'warning'
    case 'low':
      return 'negative'
    default:
      return 'grey'
  }
})

function formatDate(value: string | null): string {
  if (!value) return 'Unknown'
  const date = parseUtc(value)
  return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleDateString()
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong'
}

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
    submitError.value = errorMessage(err)
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
  if (!value) emit('closed')
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
