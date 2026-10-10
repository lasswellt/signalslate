// Source of truth: api/routers/packages.py (Pydantic Out/Body models). The request helper below is
// copied from useDomainsApi.ts (itself a copy of useApi.ts's private request()/toApiError()) so this
// file sends the same CSRF header, JSON body and error normalization, and a coded detail
// ({code, message}, e.g. package_exists, unrecognized_tracking_number, scan_failed) becomes ApiError.code.
import { ApiError } from '~/composables/useApi'

export type PackageStatus =
  | 'pending'
  | 'in_transit'
  | 'out_for_delivery'
  | 'delivered'
  | 'exception'
  | 'returned'

// api/routers/packages.py PackageOut. Timestamps are ISO-8601 UTC strings.
export interface PackageOut {
  id: number
  carrier: string
  tracking_number: string | null
  merchant: string | null
  order_id: string | null
  status: PackageStatus
  status_source: string
  eta: string | null
  delivered_at: string | null
  last_event_at: string | null
  last_checked_at: string | null
  last_error: string | null
  archived: boolean
  tracking_url: string | null
  last_event: string | null
}

export interface PackageEventOut {
  occurred_at: string | null
  status: string
  description: string | null
  location: string | null
  source: string
}

// Metadata only: email bodies are never stored.
export interface PackageEmailOut {
  account_label: string
  subject: string | null
  sender: string | null
  received_at: string | null
}

export interface PackageDetailOut extends PackageOut {
  events: PackageEventOut[]
  emails: PackageEmailOut[]
}

// api/routers/packages.py SummaryOut
export interface PackageSummary {
  out_for_delivery_today: number
  arriving_today: number
  delivered_today: number
  in_transit: number
  exceptions: number
  generated_at: string
}

// api/routers/packages.py CarrierOut
export interface CarrierStatus {
  code: string
  configured: boolean
}

// api/routers/packages.py ScanOut (one per Gmail account)
export interface ScanResult {
  label: string
  scanned: number
  packages_touched: number
  errors: string[]
}

// api/routers/packages.py RefreshOut
export interface RefreshResult {
  polled: number
  changed: number
  errors: Record<string, string>
}

export interface ListPackagesParams {
  status?: PackageStatus
  carrier?: string
  include_archived?: boolean
  q?: string
}

export interface AddPackageBody {
  tracking_number: string
  carrier?: string
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
 * Typed client for the package tracking routes (api/routers/packages.py). Holds no state.
 * @returns The API methods.
 * @throws ApiError from any method when the request fails.
 */
export function usePackagesApi() {
  const { public: { apiBase } } = useRuntimeConfig()
  const url = (path: string) => `${apiBase}/api/packages${path}`

  return {
    listPackages: (params: ListPackagesParams = {}) =>
      request<PackageOut[]>(url(''), {
        params: { status: params.status, carrier: params.carrier, include_archived: params.include_archived, q: params.q },
      }),
    getPackage: (id: number) => request<PackageDetailOut>(url(`/${seg(String(id))}`)),
    addPackage: (payload: AddPackageBody) => request<PackageOut>(url(''), { method: 'POST', body: payload }),
    setArchived: (id: number, archived: boolean) =>
      request<PackageOut>(url(`/${seg(String(id))}`), { method: 'PATCH', body: { archived } }),
    deletePackage: (id: number) => request<void>(url(`/${seg(String(id))}`), { method: 'DELETE' }),
    scan: () => request<ScanResult[]>(url('/scan'), { method: 'POST' }),
    refresh: () => request<RefreshResult>(url('/refresh'), { method: 'POST' }),
    // GET /api/packages/summary
    summary: () => request<PackageSummary>(url('/summary')),
    carriers: () => request<CarrierStatus[]>(url('/carriers')),
  }
}
