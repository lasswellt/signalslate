export interface SourceHealth {
  source: string
  status: 'ok' | 'error'
  detail: string
  checked_at: string
}

export interface RunSummary {
  id: number
  // 'manual-source' is a single-source run started from the collectors page.
  trigger: 'manual' | 'scheduled' | 'manual-source'
  status: 'running' | 'success' | 'partial' | 'failed'
  started_at: string
  finished_at: string | null
  summary: string | null
  error: string | null
  has_pdf: boolean
}

export interface RunDetail extends RunSummary {
  source_health: SourceHealth[]
}

export interface StatusResponse {
  last_run: {
    id: number
    trigger: string
    status: string
    started_at: string
    finished_at: string | null
    summary: string | null
    error: string | null
  } | null
  next_scheduled_run: string | null
  source_health: SourceHealth[]
}

export interface DigestConfig {
  schedule_cron: string
  tracker: string
  active_sources: Record<string, boolean>
}

export type ConnectionKind = 'm365' | 'zoom' | 'slack' | 'gmail'
export type OAuthProvider = 'google' | 'microsoft'
export type OAuthMode = 'paste_back' | 'callback'

// Create payloads mirror api/routers/connections.py. Secrets are write-only: they appear here and
// in updates, never in a ConnectionView (which only names them in secrets_set).
export interface M365Create {
  kind: 'm365'
  alias: string
  tenant_id: string
  client_id: string
}

export interface ZoomCreate {
  kind: 'zoom'
  account_id: string
  client_id: string
  client_secret: string
}

export interface SlackCreate {
  kind: 'slack'
  label: string
  token: string
}

export interface GmailCreate {
  kind: 'gmail'
  label: string
  client_id: string
  client_secret: string
  refresh_token?: string
  redirect_mode?: OAuthMode
}

export type ConnectionCreate = M365Create | ZoomCreate | SlackCreate | GmailCreate

// An update replaces the config fields sent and only the secrets sent. The label field (alias,
// label) is immutable, and an empty-string secret means "leave as is".
export interface M365Update {
  config?: { tenant_id?: string; client_id?: string }
}

export interface ZoomUpdate {
  config?: { account_id?: string; client_id?: string }
  secrets?: { client_secret?: string }
}

export interface SlackUpdate {
  secrets?: { token?: string }
}

export interface GmailUpdate {
  config?: { client_id?: string; redirect_mode?: OAuthMode }
  secrets?: { client_secret?: string; refresh_token?: string }
}

export type ConnectionUpdate = M365Update | ZoomUpdate | SlackUpdate | GmailUpdate

export interface ConnectionHealth {
  status: string
  detail: string | null
  checked_at: string | null
}

export interface ConnectionView {
  id: string
  kind: ConnectionKind
  label: string
  origin: 'ui' | 'env'
  config: Record<string, string>
  secrets_set: string[]
  active: boolean
  health: ConnectionHealth | null
}

export interface ConnectionCheck {
  status: 'ok' | 'error'
  detail: string
}

export interface SystemInfo {
  secret_key_configured: boolean
  store_active: boolean
  public_base_url_configured: boolean
  web_origins: string[]
  oauth: Record<OAuthProvider, { modes: OAuthMode[] }>
}

export interface CollectorAttempt {
  at: string
  status: string | null
  detail: string | null
  item_count: number | null
}

export interface CollectorState {
  source: string
  active: boolean
  watermark: string | null
  consecutive_failures: number
  stuck_threshold: number
  last_attempt: CollectorAttempt | null
  item_count: number
}

export interface DryRunItem {
  item_type: string
  occurred_at: string
  external_id: string
  preview: string
}

export interface DryRunResult {
  source: string
  status: string
  detail: string
  window: { since: string; until: string; hours: number }
  count: number
  by_type: Record<string, number>
  items: DryRunItem[]
  duration_ms: number
}

export interface DryRunJob {
  status: 'running' | 'done' | 'failed'
  result: DryRunResult | null
}

export interface ResetResult {
  source: string
  watermark: string | null
  note: string
}

export interface ClearFailuresResult {
  source: string
  consecutive_failures: number
}

export interface ItemRow {
  id: number
  item_type: string
  external_id: string
  occurred_at: string
  preview: string
}

export interface ItemPage {
  items: ItemRow[]
  next_before_id: number | null
  total: number
}

export interface ItemDetail {
  id: number
  item_type: string
  external_id: string
  occurred_at: string
  payload: string
  truncated: boolean
}

export interface OAuthStart {
  flow_id: string
  auth_url: string
  mode: OAuthMode
  expires_at: string
  instructions: string
}

export interface OAuthPasteResult {
  status: string
  connection: ConnectionView
  account: string | null
}

/**
 * A failed API call. `message` is built only from the response's `detail` (its `msg` / `message`
 * text), never from the request or from the underlying fetch error, so a submitted secret cannot
 * reach a toast or a log line through it. The original error is deliberately not kept as `cause`:
 * it carries the request options, body included.
 */
export class ApiError extends Error {
  readonly status: number
  readonly code?: string

  constructor(status: number, message: string, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/**
 * Parses a timestamp from the API as UTC. The API sends `...Z`; /api/runs still sends the stored
 * naive UTC value without a zone, which `new Date` would read as local time, so a missing zone is
 * treated as UTC too.
 * @param iso - ISO 8601 timestamp, with or without a zone designator.
 * @returns The instant it denotes (an invalid Date if the text is not a timestamp).
 */
export function parseUtc(iso: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso)
  const isDateTime = /T\d{2}:\d{2}/.test(iso)
  return new Date(isDateTime && !hasZone ? `${iso}Z` : iso)
}

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

type Query = Record<string, string | number | undefined>

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  params?: Query
  // Only the OAuth start/paste calls set this: the nonce cookie set by start must round-trip
  // cross-origin, and every other call is cookie-free so no ambient credential can ride along.
  credentials?: 'include'
}

/**
 * The one place a request is made. Every non-GET carries X-Requested-With (the server's CSRF guard
 * rejects a write without it, body or not) and a JSON body, and every failure leaves as an ApiError.
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
      ...(options.credentials ? { credentials: options.credentials } : {}),
    })
  } catch (error) {
    throw toApiError(error)
  }
}

const seg = encodeURIComponent

/**
 * Typed client for the SignalSlate API. Holds no state: credentials pass through the arguments of
 * one call and are never stored or logged here.
 * @returns The API methods.
 * @throws ApiError from any method when the request fails.
 */
export function useApi() {
  const { public: { apiBase } } = useRuntimeConfig()
  const url = (path: string) => `${apiBase}/api${path}`

  return {
    getStatus: () => request<StatusResponse>(url('/status')),
    getRuns: (limit = 50) => request<RunSummary[]>(url('/runs'), { params: { limit } }),
    getRun: (id: number) => request<RunDetail>(url(`/runs/${id}`)),
    triggerRun: () => request<{ accepted: boolean }>(url('/runs/trigger'), { method: 'POST' }),
    getConfig: () => request<DigestConfig>(url('/config')),
    updateConfig: (payload: DigestConfig) => request<DigestConfig>(url('/config'), { method: 'PUT', body: payload }),
    pdfUrl: (id: number) => url(`/runs/${id}/pdf`),

    getSystem: () => request<SystemInfo>(url('/system')),

    listConnections: () => request<ConnectionView[]>(url('/connections')),
    createConnection: (payload: ConnectionCreate) =>
      request<ConnectionView>(url('/connections'), { method: 'POST', body: payload }),
    updateConnection: (id: string, payload: ConnectionUpdate) =>
      request<ConnectionView>(url(`/connections/${seg(id)}`), { method: 'PATCH', body: payload }),
    deleteConnection: (id: string) =>
      request<void>(url(`/connections/${seg(id)}`), { method: 'DELETE' }),
    testConnection: (id: string) =>
      request<ConnectionCheck>(url(`/connections/${seg(id)}/test`), { method: 'POST' }),

    listCollectors: () => request<CollectorState[]>(url('/collectors')),
    runCollector: (source: string) =>
      request<{ accepted: boolean }>(url(`/collectors/${seg(source)}/run`), { method: 'POST' }),
    startDryRun: (source: string, options: { hours?: number; limit?: number } = {}) =>
      request<{ job_id: string }>(url(`/collectors/${seg(source)}/dry-run`), { method: 'POST', body: options }),
    getDryRun: (jobId: string) => request<DryRunJob>(url(`/collectors/dry-run/${seg(jobId)}`)),
    resetCollector: (source: string, daysBack?: number) =>
      request<ResetResult>(url(`/collectors/${seg(source)}/reset`), {
        method: 'POST',
        body: { days_back: daysBack ?? null },
      }),
    clearFailures: (source: string) =>
      request<ClearFailuresResult>(url(`/collectors/${seg(source)}/clear-failures`), { method: 'POST' }),
    listItems: (source: string, options: { limit?: number; beforeId?: number; itemType?: string } = {}) =>
      request<ItemPage>(url(`/collectors/${seg(source)}/items`), {
        params: { limit: options.limit, before_id: options.beforeId, item_type: options.itemType },
      }),
    getItem: (source: string, id: number) =>
      request<ItemDetail>(url(`/collectors/${seg(source)}/items/${id}`)),

    oauthStart: (provider: OAuthProvider, payload: { connection_id: string; mode: OAuthMode }) =>
      request<OAuthStart>(url(`/oauth/${seg(provider)}/start`), {
        method: 'POST',
        body: payload,
        credentials: 'include',
      }),
    oauthPaste: (provider: OAuthProvider, payload: { flow_id: string; url: string }) =>
      request<OAuthPasteResult>(url(`/oauth/${seg(provider)}/paste`), {
        method: 'POST',
        body: payload,
        credentials: 'include',
      }),
  }
}
