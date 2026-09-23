<template>
  <div data-testid="jobs-profile-panel">
    <AsyncState :loading="loading" :error="loadError" @retry="loadProfile">
      <div class="q-gutter-md profile-body">
        <q-card>
          <q-card-section class="q-gutter-sm">
            <div class="text-subtitle1">Contact</div>
            <q-input v-model="form.full_name" dense outlined label="Full name" data-testid="jobs-full-name" />
            <q-input
              v-model="form.email"
              dense
              outlined
              label="Email"
              :error="!emailValid"
              error-message="Enter a valid email address"
              data-testid="jobs-email"
            />
            <q-input v-model="form.phone" dense outlined label="Phone" data-testid="jobs-phone" />
          </q-card-section>
        </q-card>

        <q-card>
          <q-card-section class="q-gutter-sm">
            <div class="text-subtitle1">Links</div>
            <q-input
              v-model="form.linkedin_url"
              dense
              outlined
              label="LinkedIn URL"
              :error="!linkedinValid"
              error-message="Must start with http:// or https://"
              data-testid="jobs-linkedin"
            />
            <q-input
              v-model="form.github_url"
              dense
              outlined
              label="GitHub URL"
              :error="!githubValid"
              error-message="Must start with http:// or https://"
              data-testid="jobs-github"
            />
            <q-input
              v-model="form.portfolio_url"
              dense
              outlined
              label="Portfolio URL"
              :error="!portfolioValid"
              error-message="Must start with http:// or https://"
              data-testid="jobs-portfolio"
            />
            <q-select
              v-model="form.other_links"
              multiple
              use-chips
              use-input
              hide-dropdown-icon
              input-debounce="0"
              new-value-mode="add-unique"
              dense
              outlined
              label="Other links"
              hint="Type a URL and press Enter"
              :error="!otherLinksValid"
              error-message="Every link must start with http:// or https://"
              data-testid="jobs-other-links"
              @new-value="createChipValue"
            />
          </q-card-section>
        </q-card>

        <q-card>
          <q-card-section class="q-gutter-sm">
            <div class="text-subtitle1">Work eligibility</div>
            <q-select
              v-model="form.work_authorized"
              :options="BOOL_OPTIONS"
              emit-value
              map-options
              dense
              outlined
              label="Authorized to work"
              data-testid="jobs-work-authorized"
            />
            <q-select
              v-model="form.needs_sponsorship"
              :options="BOOL_OPTIONS"
              emit-value
              map-options
              dense
              outlined
              label="Needs sponsorship"
              data-testid="jobs-needs-sponsorship"
            />
            <q-select
              v-model="form.open_to_relocation"
              :options="BOOL_OPTIONS"
              emit-value
              map-options
              dense
              outlined
              label="Open to relocation"
              data-testid="jobs-open-to-relocation"
            />
            <q-input v-model="form.relocation_notes" dense outlined label="Relocation notes" data-testid="jobs-relocation-notes" />
            <q-input v-model="form.start_date_notes" dense outlined label="Start date notes" data-testid="jobs-start-date-notes" />

            <q-separator />
            <div class="text-caption text-grey-8">
              EEO / demographic questions — optional, default to "Decline to answer".
            </div>
            <q-select
              v-for="question in EEO_QUESTIONS"
              :key="question.key"
              v-model="form.eeo_answers[question.key]"
              :options="question.options"
              dense
              outlined
              :label="question.label"
              :data-testid="`jobs-eeo-${question.key}`"
            />
          </q-card-section>
        </q-card>

        <q-card>
          <q-card-section class="q-gutter-sm">
            <div class="text-subtitle1">Compensation</div>
            <q-input
              v-model="form.salary_floor"
              dense
              outlined
              label="Salary floor"
              hint="Minimum you'll accept (shared on applications if policy allows)"
              :error="!salaryFloorValid"
              error-message="Enter a non-negative number"
              data-testid="jobs-salary-floor"
            />
            <q-select
              v-model="form.salary_disclosure_policy"
              :options="SALARY_POLICY_OPTIONS"
              emit-value
              map-options
              dense
              outlined
              label="Salary disclosure policy"
              data-testid="jobs-salary-disclosure-policy"
            />
          </q-card-section>
        </q-card>

        <q-card>
          <q-card-section class="q-gutter-sm">
            <div class="text-subtitle1">Targets</div>
            <q-select
              v-model="form.target_roles"
              multiple
              use-chips
              use-input
              hide-dropdown-icon
              input-debounce="0"
              new-value-mode="add-unique"
              dense
              outlined
              label="Target roles"
              hint="Type a role and press Enter"
              data-testid="jobs-target-roles"
              @new-value="createChipValue"
            />
            <q-select
              v-model="form.target_locations"
              multiple
              use-chips
              use-input
              hide-dropdown-icon
              input-debounce="0"
              new-value-mode="add-unique"
              dense
              outlined
              label="Target locations"
              hint="Type a location and press Enter"
              data-testid="jobs-target-locations"
              @new-value="createChipValue"
            />
            <q-select
              v-model="form.target_remote"
              :options="BOOL_OPTIONS"
              emit-value
              map-options
              dense
              outlined
              label="Target remote"
              data-testid="jobs-target-remote"
            />
            <q-input
              v-model="form.target_salary_floor"
              dense
              outlined
              label="Target salary floor"
              hint="Minimum for matching openings"
              :error="!targetSalaryFloorValid"
              error-message="Enter a non-negative number"
              data-testid="jobs-target-salary-floor"
            />
            <q-select
              v-model="form.target_exclusions"
              multiple
              use-chips
              use-input
              hide-dropdown-icon
              input-debounce="0"
              new-value-mode="add-unique"
              dense
              outlined
              label="Exclude company/industry"
              hint="Type a name and press Enter"
              data-testid="jobs-target-exclusions"
              @new-value="createChipValue"
            />
            <q-input v-model="form.cover_letter_tone" dense outlined label="Cover letter tone" data-testid="jobs-cover-letter-tone" />
          </q-card-section>
        </q-card>

        <q-card>
          <q-card-section class="q-gutter-sm">
            <div class="text-subtitle1">Resume</div>
            <q-file
              v-model="resumeFile"
              dense
              outlined
              accept=".pdf,application/pdf"
              label="Choose a resume file"
              data-testid="jobs-resume-file"
              @update:model-value="resumeError = null"
            />
            <q-btn
              flat
              no-caps
              color="primary"
              label="Upload"
              :loading="resumeUploading"
              :disable="!resumeFile"
              data-testid="jobs-resume-upload"
              @click="onResumeUpload"
            />
            <q-banner v-if="resumeError" dense class="bg-negative text-white" data-testid="jobs-resume-error">
              {{ resumeError }}
            </q-banner>
            <div v-if="resumeFiles.length" data-testid="jobs-resume-files">
              <div v-for="entry in resumeFiles" :key="entry.path" class="text-body2">
                {{ entry.name }} — Uploaded {{ relativeTime(entry.uploadedAt) }}
              </div>
            </div>
            <div v-else class="text-grey-8" data-testid="jobs-resume-empty">No resume uploaded yet.</div>
          </q-card-section>
        </q-card>

        <q-card>
          <q-card-section class="q-gutter-sm">
            <div class="text-subtitle1">Answer bank</div>
            <q-input v-model="answerSearch" dense outlined label="Search questions" data-testid="jobs-answer-search" />
            <q-banner v-if="answersError" dense class="bg-negative text-white" data-testid="jobs-answer-bank-error">
              {{ answersError }}
            </q-banner>
            <q-banner v-if="editError" dense class="bg-negative text-white" data-testid="jobs-answer-edit-error">
              {{ editError }}
            </q-banner>
            <AsyncState :loading="answersLoading" :empty="filteredAnswers.length === 0" skeleton="table">
              <template #empty>
                <EmptyState icon="question_answer" title="No saved answers yet" message="Answers you save during an application appear here." />
              </template>
              <div style="overflow-x: auto">
                <q-markup-table dense flat data-testid="jobs-answer-bank">
                  <thead>
                    <tr>
                      <th class="text-left">Question</th>
                      <th class="text-left">Answer</th>
                      <th class="text-left">Updated</th>
                      <th class="text-left">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in filteredAnswers" :key="row.id" :data-testid="`jobs-answer-row-${row.id}`">
                      <td>{{ row.question_raw }}</td>
                      <td>
                        <q-input
                          v-if="editingId === row.id"
                          v-model="editingAnswer"
                          type="textarea"
                          dense
                          outlined
                          :data-testid="`jobs-answer-edit-input-${row.id}`"
                        />
                        <span v-else>{{ row.answer }}</span>
                      </td>
                      <td>{{ formatDate(row.updated_at) }}</td>
                      <td>
                        <template v-if="editingId === row.id">
                          <q-btn
                            flat
                            dense
                            no-caps
                            color="primary"
                            label="Save"
                            :loading="editSaving"
                            :data-testid="`jobs-answer-save-${row.id}`"
                            @click="saveEdit(row)"
                          />
                          <q-btn flat dense no-caps label="Cancel" :data-testid="`jobs-answer-cancel-${row.id}`" @click="cancelEdit" />
                        </template>
                        <q-btn
                          v-else
                          flat
                          dense
                          no-caps
                          label="Edit"
                          :data-testid="`jobs-answer-edit-${row.id}`"
                          @click="startEdit(row)"
                        />
                      </td>
                    </tr>
                  </tbody>
                </q-markup-table>
              </div>
            </AsyncState>
          </q-card-section>
        </q-card>
      </div>
    </AsyncState>

    <div v-if="!loading && !loadError" class="profile-footer">
      <q-btn flat no-caps label="Discard" :disable="!dirty || saving" data-testid="jobs-profile-discard" @click="onDiscard" />
      <q-btn
        color="primary"
        no-caps
        label="Save"
        :loading="saving"
        :disable="!canSave"
        data-testid="jobs-profile-save"
        @click="onSave"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave } from 'vue-router'
import { useJobsApi } from '~/composables/useJobsApi'
import type { AnswerOut, JobsProfileOut, JobsProfileUpdate } from '~/composables/useJobsApi'
import AsyncState from '~/components/ui/AsyncState.vue'
import EmptyState from '~/components/ui/EmptyState.vue'

const jobsApi = useJobsApi()
const $q = useQuasar()

function emptyToNull(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim()
  return trimmed ? trimmed : null
}

// Tri-state Yes/No/unspecified picker shared by every boolean JobsProfile field.
const BOOL_OPTIONS = [
  { label: 'Yes', value: true },
  { label: 'No', value: false },
  { label: 'Not specified', value: null },
]

const SALARY_POLICY_OPTIONS = [
  { label: 'Decline to state', value: 'decline' },
  { label: 'Provide a range', value: 'range' },
  { label: 'Provide an exact figure', value: 'exact' },
]

const DECLINE = 'Decline to answer'

// JobsProfile.eeo_answers is a free-form JSON blob (pipeline/db.py), not fixed columns, so this
// panel fixes a small, common set of EEO/demographic questions with "Decline to answer" as the
// default for each. Extending this list only adds a new key to the same JSON object server-side.
const EEO_QUESTIONS: Array<{ key: string; label: string; options: string[] }> = [
  {
    key: 'race_ethnicity',
    label: 'Race/ethnicity',
    options: [
      DECLINE,
      'American Indian or Alaska Native',
      'Asian',
      'Black or African American',
      'Hispanic or Latino',
      'Native Hawaiian or Other Pacific Islander',
      'White',
      'Two or more races',
    ],
  },
  { key: 'gender', label: 'Gender', options: [DECLINE, 'Male', 'Female', 'Non-binary'] },
  { key: 'veteran_status', label: 'Veteran status', options: [DECLINE, 'I am a veteran', 'I am not a veteran'] },
  {
    key: 'disability_status',
    label: 'Disability status',
    options: [DECLINE, 'Yes, I have a disability', 'No, I do not have a disability'],
  },
]

/** Shared @new-value handler for every free-tag q-select in this panel: adds the typed text as a
 * new chip (deduplicated) on Enter/blur, per Quasar's new-value-mode="add-unique" contract. */
function createChipValue(val: string, done: (item?: string, mode?: 'add-unique') => void) {
  const trimmed = val.trim()
  if (trimmed) done(trimmed, 'add-unique')
  else done()
}

interface FormState {
  full_name: string | null
  email: string | null
  phone: string | null
  linkedin_url: string | null
  github_url: string | null
  portfolio_url: string | null
  other_links: string[]
  work_authorized: boolean | null
  needs_sponsorship: boolean | null
  open_to_relocation: boolean | null
  relocation_notes: string | null
  start_date_notes: string | null
  salary_floor: string | null
  salary_disclosure_policy: string
  eeo_answers: Record<string, string>
  target_roles: string[]
  target_locations: string[]
  target_remote: boolean | null
  target_salary_floor: string | null
  target_exclusions: string[]
  cover_letter_tone: string | null
}

function emptyForm(): FormState {
  return {
    full_name: null,
    email: null,
    phone: null,
    linkedin_url: null,
    github_url: null,
    portfolio_url: null,
    other_links: [],
    work_authorized: null,
    needs_sponsorship: null,
    open_to_relocation: null,
    relocation_notes: null,
    start_date_notes: null,
    salary_floor: null,
    salary_disclosure_policy: 'decline',
    eeo_answers: Object.fromEntries(EEO_QUESTIONS.map((q) => [q.key, DECLINE])),
    target_roles: [],
    target_locations: [],
    target_remote: null,
    target_salary_floor: null,
    target_exclusions: [],
    cover_letter_tone: null,
  }
}

const form = reactive<FormState>(emptyForm())
const original = ref<FormState | null>(null)

/** JobsProfile.other_links is a JSON list of plain URL strings or {label, url} objects; this panel
 * only edits plain URL strings, so an object entry is reduced to its `url` for display/editing. */
function toLinkString(link: unknown): string {
  if (typeof link === 'string') return link
  if (link && typeof link === 'object' && typeof (link as Record<string, unknown>).url === 'string') {
    return (link as Record<string, unknown>).url as string
  }
  return String(link)
}

// The API has no per-resume upload timestamp, only a running resume_paths list; a path already on
// the loaded profile shows the profile's own updated_at as its best-known "uploaded" time, while a
// path uploaded during this session shows the moment the upload actually completed.
const sessionUploadTimes = ref<Record<string, string>>({})
const profileUpdatedAt = ref<string | null>(null)
const resumePaths = ref<string[]>([])

const resumeFiles = computed(() =>
  resumePaths.value.map((path) => ({
    path,
    name: path.split('/').pop() || path,
    uploadedAt: sessionUploadTimes.value[path] ?? profileUpdatedAt.value,
  })),
)

function formFromProfile(profile: JobsProfileOut): FormState {
  return {
    full_name: profile.full_name,
    email: profile.email,
    phone: profile.phone,
    linkedin_url: profile.linkedin_url,
    github_url: profile.github_url,
    portfolio_url: profile.portfolio_url,
    other_links: profile.other_links.map(toLinkString),
    work_authorized: profile.work_authorized,
    needs_sponsorship: profile.needs_sponsorship,
    open_to_relocation: profile.open_to_relocation,
    relocation_notes: profile.relocation_notes,
    start_date_notes: profile.start_date_notes,
    salary_floor: profile.salary_floor,
    salary_disclosure_policy: profile.salary_disclosure_policy,
    eeo_answers: Object.fromEntries(
      EEO_QUESTIONS.map((q) => {
        const value = profile.eeo_answers[q.key]
        return [q.key, typeof value === 'string' && value ? value : DECLINE]
      }),
    ),
    target_roles: [...profile.target_roles],
    target_locations: [...profile.target_locations],
    target_remote: profile.target_remote,
    target_salary_floor: profile.target_salary_floor,
    target_exclusions: [...profile.target_exclusions],
    cover_letter_tone: profile.cover_letter_tone,
  }
}

function applyProfile(profile: JobsProfileOut) {
  const next = formFromProfile(profile)
  Object.assign(form, next)
  original.value = JSON.parse(JSON.stringify(next))
  resumePaths.value = [...profile.resume_paths]
  profileUpdatedAt.value = profile.updated_at
}

const loading = ref(true)
const loadError = ref<string | null>(null)

/**
 * Loads the singleton JobsProfile and populates the form.
 * @returns Nothing; sets loading/loadError/form state.
 */
async function loadProfile() {
  loading.value = true
  loadError.value = null
  try {
    applyProfile(await jobsApi.getProfile())
  } catch (err) {
    loadError.value = errorText(err)
  } finally {
    loading.value = false
  }
}

const dirty = computed(() => original.value !== null && JSON.stringify(form) !== JSON.stringify(original.value))

function isValidUrl(value: string | null): boolean {
  return !value || /^https?:\/\//i.test(value)
}

function isValidSalary(value: string | null): boolean {
  if (!value || !value.trim()) return true
  const n = Number(value.trim())
  return Number.isFinite(n) && n >= 0
}

const emailValid = computed(() => !form.email || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email))
const linkedinValid = computed(() => isValidUrl(form.linkedin_url))
const githubValid = computed(() => isValidUrl(form.github_url))
const portfolioValid = computed(() => isValidUrl(form.portfolio_url))
const otherLinksValid = computed(() => form.other_links.every((link) => isValidUrl(link)))
const salaryFloorValid = computed(() => isValidSalary(form.salary_floor))
const targetSalaryFloorValid = computed(() => isValidSalary(form.target_salary_floor))

const formValid = computed(
  () =>
    emailValid.value &&
    linkedinValid.value &&
    githubValid.value &&
    portfolioValid.value &&
    otherLinksValid.value &&
    salaryFloorValid.value &&
    targetSalaryFloorValid.value,
)

const saving = ref(false)
const canSave = computed(() => dirty.value && formValid.value && !saving.value)

/**
 * Sends the full editable JobsProfile form via PUT /jobs/profile.
 * @returns Nothing; sets saving state, notifies success/failure, and re-applies the server's response.
 */
async function onSave() {
  if (!canSave.value) return
  saving.value = true
  const payload: JobsProfileUpdate = {
    full_name: emptyToNull(form.full_name),
    email: emptyToNull(form.email),
    phone: emptyToNull(form.phone),
    linkedin_url: emptyToNull(form.linkedin_url),
    github_url: emptyToNull(form.github_url),
    portfolio_url: emptyToNull(form.portfolio_url),
    other_links: [...form.other_links],
    work_authorized: form.work_authorized,
    needs_sponsorship: form.needs_sponsorship,
    open_to_relocation: form.open_to_relocation,
    relocation_notes: emptyToNull(form.relocation_notes),
    start_date_notes: emptyToNull(form.start_date_notes),
    salary_floor: emptyToNull(form.salary_floor),
    salary_disclosure_policy: form.salary_disclosure_policy,
    eeo_answers: { ...form.eeo_answers },
    target_roles: [...form.target_roles],
    target_locations: [...form.target_locations],
    target_remote: form.target_remote,
    target_salary_floor: emptyToNull(form.target_salary_floor),
    target_exclusions: [...form.target_exclusions],
    cover_letter_tone: emptyToNull(form.cover_letter_tone),
  }
  try {
    applyProfile(await jobsApi.updateProfile(payload))
    $q.notify({ type: 'positive', message: 'Profile saved.' })
  } catch (err) {
    $q.notify({ type: 'negative', message: errorText(err) })
  } finally {
    saving.value = false
  }
}

function onDiscard() {
  if (!original.value) return
  Object.assign(form, JSON.parse(JSON.stringify(original.value)))
}

/** Shows the discard-changes confirm via the Quasar Dialog plugin; resolves true only on Discard. */
function confirmDiscard(): Promise<boolean> {
  return new Promise((resolve) => {
    $q.dialog({
      title: 'Discard unsaved changes?',
      message: 'Your profile changes have not been saved.',
      persistent: true,
      cancel: { label: 'Keep editing', flat: true, noCaps: true },
      ok: { label: 'Discard', color: 'negative', noCaps: true },
    })
      .onOk(() => resolve(true))
      .onCancel(() => resolve(false))
      .onDismiss(() => resolve(false))
  })
}

// Leave guard: onBeforeRouteLeave no-ops when this panel is mounted outside a <RouterView> (e.g. the
// unit-test harness), which is acceptable per T-009's note — the beforeunload listener below still
// warns on a real tab close/reload either way, and the dirty state + Save/Discard buttons are tested
// directly instead of relying on navigation.
onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  return await confirmDiscard()
})

function onBeforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  window.addEventListener('beforeunload', onBeforeUnload)
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
})

// Resume upload
const resumeFile = ref<File | null>(null)
const resumeUploading = ref(false)
const resumeError = ref<string | null>(null)

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const commaIndex = result.indexOf(',')
      resolve(commaIndex >= 0 ? result.slice(commaIndex + 1) : result)
    }
    reader.onerror = () => reject(new Error('Failed to read the selected file'))
    reader.readAsDataURL(file)
  })
}

/**
 * Uploads the chosen resume file (base64-encoded client-side, per T-023's `{filename,
 * content_b64}` body) via POST /jobs/profile/resume.
 * @returns Nothing; sets resumeUploading/resumeError/resumePaths state.
 */
async function onResumeUpload() {
  const file = resumeFile.value
  if (!file) return
  resumeError.value = null
  resumeUploading.value = true
  try {
    const contentB64 = await readFileAsBase64(file)
    const out = await jobsApi.uploadResume({ filename: file.name, content_b64: contentB64 })
    const addedPath = out.resume_paths.find((p) => !resumePaths.value.includes(p))
    if (addedPath) sessionUploadTimes.value = { ...sessionUploadTimes.value, [addedPath]: new Date().toISOString() }
    resumePaths.value = out.resume_paths
    resumeFile.value = null
  } catch (err) {
    resumeError.value = errorText(err)
  } finally {
    resumeUploading.value = false
  }
}

// Answer bank. api/routers/job_apply.py exposes only GET and POST /jobs/answers (no PUT/PATCH,
// no DELETE) — see pipeline/db.py AnswerBank's own docstring: more than one row per question is
// expected as an answer evolves, and a caller always picks the newest/best match itself. So "Edit"
// below still creates a new row via the existing POST (the only available primitive), but the list
// this panel renders is deduped to the newest row per normalized question, so the user only ever
// sees one — effectively an update from their point of view, without inventing an API this backend
// doesn't have.
const answers = ref<AnswerOut[]>([])
const answersLoading = ref(false)
const answersError = ref<string | null>(null)
const answerSearch = ref('')

async function loadAnswers() {
  answersLoading.value = true
  answersError.value = null
  try {
    answers.value = await jobsApi.listAnswers()
  } catch (err) {
    answersError.value = errorText(err)
  } finally {
    answersLoading.value = false
  }
}

const latestAnswers = computed(() => {
  const byQuestion = new Map<string, AnswerOut>()
  for (const row of answers.value) {
    const existing = byQuestion.get(row.question_norm)
    if (!existing || row.id > existing.id) byQuestion.set(row.question_norm, row)
  }
  return [...byQuestion.values()]
})

const filteredAnswers = computed(() => {
  const term = answerSearch.value.trim().toLowerCase()
  if (!term) return latestAnswers.value
  return latestAnswers.value.filter((row) => row.question_raw.toLowerCase().includes(term))
})

const editingId = ref<number | null>(null)
const editingAnswer = ref('')
const editSaving = ref(false)
const editError = ref<string | null>(null)

function startEdit(row: AnswerOut) {
  editingId.value = row.id
  editingAnswer.value = row.answer
  editError.value = null
}

function cancelEdit() {
  editingId.value = null
  editingAnswer.value = ''
}

async function saveEdit(row: AnswerOut) {
  editSaving.value = true
  editError.value = null
  try {
    const created = await jobsApi.createAnswer({ question_raw: row.question_raw, answer: editingAnswer.value })
    answers.value = [created, ...answers.value]
    editingId.value = null
    editingAnswer.value = ''
  } catch (err) {
    editError.value = errorText(err)
  } finally {
    editSaving.value = false
  }
}

onMounted(() => {
  void loadProfile()
  void loadAnswers()
})
</script>

<style scoped lang="scss">
.profile-body {
  padding-bottom: 72px;
}

.profile-footer {
  position: sticky;
  bottom: 0;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 0;
  background: var(--ss-surface-bg, #f6f7f9);
  border-top: 1px solid var(--ss-border, #e3e6ea);
}
</style>
