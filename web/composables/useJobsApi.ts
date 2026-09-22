// Mirrors api/routers/jobs.py and api/routers/job_apply.py. Split from useApi.ts the same way
// useDomainsApi.ts was: the request helper below is copied from useApi.ts's own (not exported)
// `request()`/`toApiError()` pair so this file stays independent of useApi.ts's internals while
// sending the exact same CSRF header, JSON body and error normalization.
import { ApiError } from '~/composables/useApi'

// api/routers/jobs.py BoardOut
export interface BoardOut {
  id: number
  ats_kind: string
  board_id: string
  resolved_by: string
  confidence: number
  verified_at: string | null
  last_polled_at: string | null
  last_error: string | null
}

export interface CompanyOut {
  id: number
  name: string
  domain: string | null
  source: string
  status: string
  first_seen: string
  last_seen: string
  boards: BoardOut[]
}

export interface AddCompanyBody {
  name: string
  domain?: string | null
}

export interface ImportBody {
  csv: string
}

export interface JobRejectedRow {
  line: number
  reason: string
}

export interface JobImportOut {
  added: string[]
  rejected: JobRejectedRow[]
}

export interface BoardOverrideBody {
  ats_kind: string
  board_id: string
}

export interface PostingOut {
  id: number
  title: string
  location: string | null
  remote: boolean | null
  comp_text: string | null
  apply_url: string
  first_seen: string
  last_seen: string
  closed_at: string | null
  fit_score: number | null
  fit_reason: string | null
  company_name: string
  ats_kind: string
}

// api/routers/job_apply.py JobsProfileOut
export interface JobsProfileOut {
  id: number
  full_name: string | null
  email: string | null
  phone: string | null
  linkedin_url: string | null
  github_url: string | null
  portfolio_url: string | null
  other_links: unknown[]
  work_authorized: boolean | null
  needs_sponsorship: boolean | null
  open_to_relocation: boolean | null
  relocation_notes: string | null
  start_date_notes: string | null
  salary_floor: string | null
  salary_disclosure_policy: string
  eeo_answers: Record<string, unknown>
  target_roles: string[]
  target_locations: string[]
  target_remote: boolean | null
  target_salary_floor: string | null
  target_exclusions: string[]
  resume_paths: string[]
  cover_letter_tone: string | null
  updated_at: string | null
}

// PUT /jobs/profile full-replace body: every JobsProfile field except id, updated_at
// (server-managed) and resume_paths (managed only by POST /jobs/profile/resume).
export interface JobsProfileUpdate {
  full_name?: string | null
  email?: string | null
  phone?: string | null
  linkedin_url?: string | null
  github_url?: string | null
  portfolio_url?: string | null
  other_links?: unknown[]
  work_authorized?: boolean | null
  needs_sponsorship?: boolean | null
  open_to_relocation?: boolean | null
  relocation_notes?: string | null
  start_date_notes?: string | null
  salary_floor?: string | null
  salary_disclosure_policy?: string
  eeo_answers?: Record<string, unknown>
  target_roles?: string[]
  target_locations?: string[]
  target_remote?: boolean | null
  target_salary_floor?: string | null
  target_exclusions?: string[]
  cover_letter_tone?: string | null
}

export interface ResumeUploadBody {
  filename: string
  content_b64: string
}

export interface ResumeUploadOut {
  resume_paths: string[]
}

export interface AnswerCreateBody {
  question_raw: string
  answer: string
}

export interface AnswerOut {
  id: number
  question_norm: string
  question_raw: string
  answer: string
  source_application_id: number | null
  updated_at: string | null
}

export type ApplicationStatus =
  | 'saved'
  | 'preparing'
  | 'ready'
  | 'submitted'
  | 'interviewing'
  | 'rejected'
  | 'closed'
  | 'withdrawn'

export interface ApplicationCreateBody {
  posting_id: number
}

// status is a plain string here (not the ApplicationStatus union): an illegal/unknown value must
// reach pipeline.jobs.apply.transition() server-side and come back as 422 illegal_transition,
// mirroring ApplicationStatusBody's own docstring.
export interface ApplicationStatusBody {
  status: string
}

export interface PacketEditBody {
  cover_letter_text: string
}

export interface ApplicationOut {
  id: number
  posting_id: number
  status: string
  packet: Record<string, unknown> | null
  cover_letter_path: string | null
  resume_path: string | null
  assist_state: string
  assist_session_id: string | null
  created_at: string | null
  submitted_at: string | null
}

export interface FilledFieldBody {
  field_id: string
  value: string
  source: string
  confidence: number
}

export interface ApprovedAnswerBody {
  question: string
  answer: string
}

// PATCH body for the desktop runner's progress reports. assist_state is a plain string, validated
// server-side against the idle/queued/claimed/running/paused/done/failed set (422
// unknown_assist_state otherwise) — deliberately never a `status` field (see api/routers/job_apply.py).
export interface AssistSessionUpdateBody {
  step?: string | null
  assist_state?: string | null
  filled_fields?: FilledFieldBody[]
  approved_answers?: ApprovedAnswerBody[]
}

// POST body for /jobs/assist/sessions/{session_id}/propose: the field descriptors the desktop
// runner's in-page extractor produced (pipeline.jobs.assist.extract.extract_fields()'s own shape,
// not this API's — see api/routers/job_apply.py's module docstring).
export interface ProposeBody {
  fields: Array<Record<string, unknown>>
}

export interface FieldProposalOut {
  field_id: string
  value: unknown
  confidence: number
  source: string
  needs_user: boolean
}

export type ApplicationFileKind = 'cover_letter' | 'resume'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

// FastAPI validation errors: `loc` starts with where the value came from, which is noise to a user.
const LOC_ORIGINS = new Set(['body', 'query', 'path', 'header', 'cookie'])

function describeValidationEntry(entry: unknown): string | null {
  if (!isRecord(entry) || typeof entry.msg !== 'string') return null
  const loc = Array.isArray(entry.loc)
    ? entry.loc.filter((part): part is string | number => typeof part === 'string' || typeof part === 'number')
    : []
  const field = (loc.length > 1 && typeof loc[0] === 'string' && LOC_ORIGINS.has(loc[0]) ? loc.slice(1) : loc).join('.')
  return field ? `${field}: ${entry.msg}` : entry.msg
}

function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  const fetchError = isRecord(error) ? error : {}
  const response = isRecord(fetchError.response) ? fetchError.response : {}
  const status = [fetchError.status, fetchError.statusCode, response.status].find(
    (value): value is number => typeof value === 'number',
  ) ?? 0
  const data = fetchError.data ?? response._data
  const detail = isRecord(data) ? data.detail : undefined

  let message: string | null = null
  let code: string | undefined
  if (typeof detail === 'string') {
    message = detail
  } else if (Array.isArray(detail)) {
    message = detail.map(describeValidationEntry).filter((line): line is string => line !== null).join('; ') || null
  } else if (isRecord(detail)) {
    if (typeof detail.message === 'string') message = detail.message
    if (typeof detail.code === 'string') code = detail.code
  }
  message ??= status === 0 ? 'The API could not be reached' : `Request failed (${status})`
  return new ApiError(status, message, code)
}

type Query = Record<string, string | number | boolean | undefined>

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  params?: Query
}

/**
 * The one place a request is made from this composable: mirrors useApi.ts's own `request()` so
 * every non-GET carries X-Requested-With and a JSON body, and every failure leaves as an ApiError.
 */
async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const mutating = method !== 'GET'
  const params = options.params
    ? Object.fromEntries(Object.entries(options.params).filter(([, value]) => value !== undefined))
    : undefined
  try {
    return await $fetch<T>(url, {
      method,
      params,
      headers: mutating ? { 'X-Requested-With': 'signalslate', 'Content-Type': 'application/json' } : undefined,
      body: mutating && options.body !== undefined ? JSON.stringify(options.body) : undefined,
    })
  } catch (error) {
    throw toApiError(error)
  }
}

const seg = encodeURIComponent

/**
 * Typed client for the jobs-collector and apply-assist routes (api/routers/jobs.py,
 * api/routers/job_apply.py). Holds no state.
 * @returns The API methods.
 * @throws ApiError from any method when the request fails.
 */
export function useJobsApi() {
  const { public: { apiBase } } = useRuntimeConfig()
  const url = (path: string) => `${apiBase}/api${path}`

  return {
    // --- Companies / boards / postings (api/routers/jobs.py) ---
    listCompanies: (options: { status?: 'active' | 'muted' } = {}) =>
      request<CompanyOut[]>(url('/jobs/companies'), { params: { status: options.status } }),
    addCompany: (payload: AddCompanyBody) =>
      request<CompanyOut>(url('/jobs/companies'), { method: 'POST', body: payload }),
    importCompanies: (payload: ImportBody) =>
      request<JobImportOut>(url('/jobs/companies/import'), { method: 'POST', body: payload }),
    importCompaniesYc: () => request<JobImportOut>(url('/jobs/companies/import-yc'), { method: 'POST' }),
    importCompaniesHn: () => request<JobImportOut>(url('/jobs/companies/import-hn'), { method: 'POST' }),
    importCompaniesInbox: () => request<JobImportOut>(url('/jobs/companies/import-inbox'), { method: 'POST' }),
    rescanCompany: (id: number) => request<CompanyOut>(url(`/jobs/companies/${seg(id)}/rescan`), { method: 'POST' }),
    overrideBoard: (id: number, payload: BoardOverrideBody) =>
      request<BoardOut>(url(`/jobs/boards/${seg(id)}`), { method: 'PUT', body: payload }),
    // GET /api/jobs/postings
    listPostings: (
      options: {
        minScore?: number
        status?: 'open' | 'closed'
        company?: string
        text?: string
        remote?: boolean
      } = {},
    ) =>
      request<PostingOut[]>(url('/jobs/postings'), {
        params: {
          min_score: options.minScore,
          status: options.status,
          company: options.company,
          text: options.text,
          remote: options.remote,
        },
      }),

    // --- Profile / resume (api/routers/job_apply.py) ---
    getProfile: () => request<JobsProfileOut>(url('/jobs/profile')),
    updateProfile: (payload: JobsProfileUpdate) =>
      request<JobsProfileOut>(url('/jobs/profile'), { method: 'PUT', body: payload }),
    uploadResume: (payload: ResumeUploadBody) =>
      request<ResumeUploadOut>(url('/jobs/profile/resume'), { method: 'POST', body: payload }),

    // --- AnswerBank ---
    listAnswers: (q?: string) => request<AnswerOut[]>(url('/jobs/answers'), { params: { q } }),
    createAnswer: (payload: AnswerCreateBody) =>
      request<AnswerOut>(url('/jobs/answers'), { method: 'POST', body: payload }),

    // --- Applications / packet: GET/POST /api/jobs/applications, PATCH /api/jobs/applications/{id} ---
    listApplications: (status?: ApplicationStatus) =>
      request<ApplicationOut[]>(url('/jobs/applications'), { params: { status } }),
    createApplication: (payload: ApplicationCreateBody) =>
      request<ApplicationOut>(url('/jobs/applications'), { method: 'POST', body: payload }),
    updateApplicationStatus: (id: number, payload: ApplicationStatusBody) =>
      request<ApplicationOut>(url(`/jobs/applications/${seg(id)}`), { method: 'PATCH', body: payload }),
    prepareApplicationPacket: (id: number) =>
      request<ApplicationOut>(url(`/jobs/applications/${seg(id)}/packet`), { method: 'POST' }),
    editApplicationPacket: (id: number, payload: PacketEditBody) =>
      request<ApplicationOut>(url(`/jobs/applications/${seg(id)}/packet`), { method: 'PUT', body: payload }),

    // --- Apply assist: queue / claim / progress / propose / files ---
    queueAssist: (id: number) =>
      request<ApplicationOut>(url(`/jobs/applications/${seg(id)}/assist`), { method: 'POST' }),
    getAssistQueueHead: () => request<ApplicationOut | null>(url('/jobs/assist/queue')),
    claimAssist: (id: number) =>
      request<ApplicationOut>(url(`/jobs/assist/queue/${seg(id)}/claim`), { method: 'POST' }),
    updateAssistSession: (sessionId: string, payload: AssistSessionUpdateBody) =>
      request<ApplicationOut>(url(`/jobs/assist/sessions/${seg(sessionId)}`), { method: 'PATCH', body: payload }),
    proposeSessionValues: (sessionId: string, payload: ProposeBody) =>
      request<FieldProposalOut[]>(url(`/jobs/assist/sessions/${seg(sessionId)}/propose`), {
        method: 'POST',
        body: payload,
      }),
    // File download endpoint: mirrors useApi.ts's pdfUrl — a URL builder, not a typed fetch call.
    applicationFileUrl: (id: number, kind: ApplicationFileKind) =>
      url(`/jobs/applications/${seg(id)}/files/${seg(kind)}`),
  }
}
