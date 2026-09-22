<template>
  <q-card data-testid="jobs-profile-panel">
    <q-card-section>
      <div class="text-h6">Jobs profile</div>
    </q-card-section>

    <q-card-section v-if="loading" data-testid="jobs-profile-loading">
      <q-skeleton type="text" width="60%" />
      <q-skeleton type="text" width="40%" />
      <q-skeleton type="text" width="50%" />
    </q-card-section>

    <q-card-section v-else-if="loadError" data-testid="jobs-profile-load-error">
      <q-banner dense class="bg-negative text-white">{{ loadError }}</q-banner>
      <q-btn flat no-caps label="Retry" data-testid="jobs-profile-load-retry" @click="loadProfile" />
    </q-card-section>

    <template v-else>
      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">Contact info</div>
        <q-input v-model="form.full_name" dense outlined label="Full name" data-testid="jobs-full-name" />
        <q-input v-model="form.email" dense outlined label="Email" data-testid="jobs-email" />
        <q-input v-model="form.phone" dense outlined label="Phone" data-testid="jobs-phone" />
      </q-card-section>

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">Links</div>
        <q-input v-model="form.linkedin_url" dense outlined label="LinkedIn URL" data-testid="jobs-linkedin" />
        <q-input v-model="form.github_url" dense outlined label="GitHub URL" data-testid="jobs-github" />
        <q-input v-model="form.portfolio_url" dense outlined label="Portfolio URL" data-testid="jobs-portfolio" />
        <div>
          <div class="row items-center q-gutter-xs">
            <q-input
              v-model="otherLinks.input.value"
              dense
              outlined
              label="Other link"
              data-testid="jobs-other-link-input"
              @keyup.enter="otherLinks.add"
            />
            <q-btn flat dense no-caps label="Add" data-testid="jobs-other-link-add" @click="otherLinks.add" />
          </div>
          <div class="row items-center q-gutter-xs q-mt-xs" data-testid="jobs-other-links">
            <q-chip
              v-for="link in otherLinks.items.value"
              :key="link"
              removable
              :data-testid="`jobs-other-link-${link}`"
              @remove="otherLinks.remove(link)"
            >
              {{ link }}
            </q-chip>
          </div>
        </div>
      </q-card-section>

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">Work authorization &amp; availability</div>
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
      </q-card-section>

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">Salary</div>
        <q-input v-model="form.salary_floor" dense outlined label="Salary floor" data-testid="jobs-salary-floor" />
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

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">EEO / demographic questions</div>
        <div class="text-caption text-grey-8">
          These are optional and default to "Decline to answer" unless filled in.
        </div>
        <q-select
          v-for="question in EEO_QUESTIONS"
          :key="question.key"
          v-model="eeoAnswers[question.key]"
          :options="question.options"
          dense
          outlined
          :label="question.label"
          :data-testid="`jobs-eeo-${question.key}`"
        />
      </q-card-section>

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">Target preferences</div>
        <div>
          <div class="row items-center q-gutter-xs">
            <q-input
              v-model="targetRoles.input.value"
              dense
              outlined
              label="Target role"
              data-testid="jobs-target-role-input"
              @keyup.enter="targetRoles.add"
            />
            <q-btn flat dense no-caps label="Add" data-testid="jobs-target-role-add" @click="targetRoles.add" />
          </div>
          <div class="row items-center q-gutter-xs q-mt-xs" data-testid="jobs-target-roles">
            <q-chip
              v-for="role in targetRoles.items.value"
              :key="role"
              removable
              :data-testid="`jobs-target-role-${role}`"
              @remove="targetRoles.remove(role)"
            >
              {{ role }}
            </q-chip>
          </div>
        </div>
        <div>
          <div class="row items-center q-gutter-xs">
            <q-input
              v-model="targetLocations.input.value"
              dense
              outlined
              label="Target location"
              data-testid="jobs-target-location-input"
              @keyup.enter="targetLocations.add"
            />
            <q-btn flat dense no-caps label="Add" data-testid="jobs-target-location-add" @click="targetLocations.add" />
          </div>
          <div class="row items-center q-gutter-xs q-mt-xs" data-testid="jobs-target-locations">
            <q-chip
              v-for="location in targetLocations.items.value"
              :key="location"
              removable
              :data-testid="`jobs-target-location-${location}`"
              @remove="targetLocations.remove(location)"
            >
              {{ location }}
            </q-chip>
          </div>
        </div>
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
        <q-input v-model="form.target_salary_floor" dense outlined label="Target salary floor" data-testid="jobs-target-salary-floor" />
        <div>
          <div class="row items-center q-gutter-xs">
            <q-input
              v-model="targetExclusions.input.value"
              dense
              outlined
              label="Exclude company/industry"
              data-testid="jobs-target-exclusion-input"
              @keyup.enter="targetExclusions.add"
            />
            <q-btn flat dense no-caps label="Add" data-testid="jobs-target-exclusion-add" @click="targetExclusions.add" />
          </div>
          <div class="row items-center q-gutter-xs q-mt-xs" data-testid="jobs-target-exclusions">
            <q-chip
              v-for="exclusion in targetExclusions.items.value"
              :key="exclusion"
              removable
              :data-testid="`jobs-target-exclusion-${exclusion}`"
              @remove="targetExclusions.remove(exclusion)"
            >
              {{ exclusion }}
            </q-chip>
          </div>
        </div>
        <q-input v-model="form.cover_letter_tone" dense outlined label="Cover letter tone" data-testid="jobs-cover-letter-tone" />
      </q-card-section>

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="row items-center q-gutter-sm">
          <q-btn color="primary" no-caps label="Save" :loading="saving" data-testid="jobs-profile-save" @click="onSave" />
          <span v-if="saved" class="text-positive" data-testid="jobs-profile-saved">Saved</span>
        </div>
        <q-banner v-if="saveError" dense class="bg-negative text-white" data-testid="jobs-profile-save-error">
          {{ saveError }}
        </q-banner>
      </q-card-section>

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">Resume</div>
        <input
          type="file"
          accept="application/pdf"
          data-testid="jobs-resume-upload"
          @change="onResumeChange"
        />
        <div v-if="resumeUploading" data-testid="jobs-resume-uploading">Uploading&hellip;</div>
        <q-banner v-if="resumeError" dense class="bg-negative text-white" data-testid="jobs-resume-error">
          {{ resumeError }}
        </q-banner>
        <div v-if="resumePaths.length" data-testid="jobs-resume-paths">
          <div v-for="path in resumePaths" :key="path">{{ path }}</div>
        </div>
        <div v-else class="text-grey-8" data-testid="jobs-resume-empty">No resume uploaded yet.</div>
      </q-card-section>

      <q-separator />
      <q-card-section class="q-gutter-sm">
        <div class="text-subtitle2">Answer bank</div>
        <q-input
          v-model="answerSearch"
          dense
          outlined
          label="Search questions"
          data-testid="jobs-answer-search"
        />
        <div v-if="answersLoading" data-testid="jobs-answer-bank-loading">
          <q-skeleton type="text" width="60%" />
        </div>
        <q-banner v-if="answersError" dense class="bg-negative text-white" data-testid="jobs-answer-bank-error">
          {{ answersError }}
        </q-banner>
        <q-banner v-if="editError" dense class="bg-negative text-white" data-testid="jobs-answer-edit-error">
          {{ editError }}
        </q-banner>
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
                <td>{{ row.updated_at ?? '—' }}</td>
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
                    <q-btn
                      flat
                      dense
                      no-caps
                      label="Cancel"
                      :data-testid="`jobs-answer-cancel-${row.id}`"
                      @click="cancelEdit"
                    />
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
      </q-card-section>
    </template>
  </q-card>
</template>

<script setup lang="ts">
import { ApiError } from '~/composables/useApi'
import { useJobsApi } from '~/composables/useJobsApi'
import type { AnswerOut, JobsProfileOut, JobsProfileUpdate } from '~/composables/useJobsApi'

const jobsApi = useJobsApi()

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong'
}

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

/** A small add/remove-chip list backing one of JobsProfile's JSON string-array fields. */
function createChipList() {
  const input = ref('')
  const items = ref<string[]>([])
  function add() {
    const value = input.value.trim()
    if (value && !items.value.includes(value)) items.value = [...items.value, value]
    input.value = ''
  }
  function remove(value: string) {
    items.value = items.value.filter((v) => v !== value)
  }
  return { input, items, add, remove }
}

const otherLinks = createChipList()
const targetRoles = createChipList()
const targetLocations = createChipList()
const targetExclusions = createChipList()

const form = reactive({
  full_name: null as string | null,
  email: null as string | null,
  phone: null as string | null,
  linkedin_url: null as string | null,
  github_url: null as string | null,
  portfolio_url: null as string | null,
  work_authorized: null as boolean | null,
  needs_sponsorship: null as boolean | null,
  open_to_relocation: null as boolean | null,
  relocation_notes: null as string | null,
  start_date_notes: null as string | null,
  salary_floor: null as string | null,
  salary_disclosure_policy: 'decline',
  target_remote: null as boolean | null,
  target_salary_floor: null as string | null,
  cover_letter_tone: null as string | null,
})

const eeoAnswers = reactive<Record<string, string>>(
  Object.fromEntries(EEO_QUESTIONS.map((q) => [q.key, DECLINE])),
)

const resumePaths = ref<string[]>([])

/** JobsProfile.other_links is a JSON list of plain URL strings or {label, url} objects; this panel
 * only edits plain URL strings, so an object entry is reduced to its `url` for display/editing. */
function toLinkString(link: unknown): string {
  if (typeof link === 'string') return link
  if (link && typeof link === 'object' && typeof (link as Record<string, unknown>).url === 'string') {
    return (link as Record<string, unknown>).url as string
  }
  return String(link)
}

function applyProfile(profile: JobsProfileOut) {
  form.full_name = profile.full_name
  form.email = profile.email
  form.phone = profile.phone
  form.linkedin_url = profile.linkedin_url
  form.github_url = profile.github_url
  form.portfolio_url = profile.portfolio_url
  form.work_authorized = profile.work_authorized
  form.needs_sponsorship = profile.needs_sponsorship
  form.open_to_relocation = profile.open_to_relocation
  form.relocation_notes = profile.relocation_notes
  form.start_date_notes = profile.start_date_notes
  form.salary_floor = profile.salary_floor
  form.salary_disclosure_policy = profile.salary_disclosure_policy
  form.target_remote = profile.target_remote
  form.target_salary_floor = profile.target_salary_floor
  form.cover_letter_tone = profile.cover_letter_tone
  otherLinks.items.value = profile.other_links.map(toLinkString)
  targetRoles.items.value = [...profile.target_roles]
  targetLocations.items.value = [...profile.target_locations]
  targetExclusions.items.value = [...profile.target_exclusions]
  resumePaths.value = [...profile.resume_paths]
  for (const question of EEO_QUESTIONS) {
    const value = profile.eeo_answers[question.key]
    eeoAnswers[question.key] = typeof value === 'string' && value ? value : DECLINE
  }
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
    loadError.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

const saving = ref(false)
const saveError = ref<string | null>(null)
const saved = ref(false)

/**
 * Sends the full editable JobsProfile form via PUT /jobs/profile.
 * @returns Nothing; sets saving/saveError/saved state and re-applies the server's response.
 */
async function onSave() {
  saving.value = true
  saveError.value = null
  saved.value = false
  const payload: JobsProfileUpdate = {
    full_name: emptyToNull(form.full_name),
    email: emptyToNull(form.email),
    phone: emptyToNull(form.phone),
    linkedin_url: emptyToNull(form.linkedin_url),
    github_url: emptyToNull(form.github_url),
    portfolio_url: emptyToNull(form.portfolio_url),
    other_links: [...otherLinks.items.value],
    work_authorized: form.work_authorized,
    needs_sponsorship: form.needs_sponsorship,
    open_to_relocation: form.open_to_relocation,
    relocation_notes: emptyToNull(form.relocation_notes),
    start_date_notes: emptyToNull(form.start_date_notes),
    salary_floor: emptyToNull(form.salary_floor),
    salary_disclosure_policy: form.salary_disclosure_policy,
    eeo_answers: { ...eeoAnswers },
    target_roles: [...targetRoles.items.value],
    target_locations: [...targetLocations.items.value],
    target_remote: form.target_remote,
    target_salary_floor: emptyToNull(form.target_salary_floor),
    target_exclusions: [...targetExclusions.items.value],
    cover_letter_tone: emptyToNull(form.cover_letter_tone),
  }
  try {
    applyProfile(await jobsApi.updateProfile(payload))
    saved.value = true
  } catch (err) {
    saveError.value = errorMessage(err)
  } finally {
    saving.value = false
  }
}

const resumeUploading = ref(false)
const resumeError = ref<string | null>(null)

/**
 * Reads the selected PDF as base64 client-side (the API only accepts JSON, per T-023's
 * `{filename, content_b64}` body) and uploads it via POST /jobs/profile/resume.
 * @param event - The file input's change event.
 * @returns Nothing; sets resumeUploading/resumeError/resumePaths state.
 */
function onResumeChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  resumeError.value = null
  resumeUploading.value = true
  const reader = new FileReader()
  reader.onload = () => {
    void (async () => {
      try {
        const result = typeof reader.result === 'string' ? reader.result : ''
        const commaIndex = result.indexOf(',')
        const contentB64 = commaIndex >= 0 ? result.slice(commaIndex + 1) : result
        const out = await jobsApi.uploadResume({ filename: file.name, content_b64: contentB64 })
        resumePaths.value = out.resume_paths
      } catch (err) {
        resumeError.value = errorMessage(err)
      } finally {
        resumeUploading.value = false
        input.value = ''
      }
    })()
  }
  reader.onerror = () => {
    resumeError.value = 'Failed to read the selected file'
    resumeUploading.value = false
    input.value = ''
  }
  reader.readAsDataURL(file)
}

// Answer bank. api/routers/job_apply.py exposes only GET and POST /jobs/answers (no PUT/PATCH,
// no DELETE) — see pipeline/db.py AnswerBank's own docstring, which documents this as deliberate:
// more than one row per question is expected as an answer evolves, and a caller always picks the
// newest/best match itself. So "edit" below creates a new row via the existing POST rather than
// mutating the old one, and there is no delete affordance since no route exists for it.
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
    answersError.value = errorMessage(err)
  } finally {
    answersLoading.value = false
  }
}

const filteredAnswers = computed(() => {
  const term = answerSearch.value.trim().toLowerCase()
  if (!term) return answers.value
  return answers.value.filter((row) => row.question_raw.toLowerCase().includes(term))
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
    editError.value = errorMessage(err)
  } finally {
    editSaving.value = false
  }
}

onMounted(() => {
  void loadProfile()
  void loadAnswers()
})
</script>
