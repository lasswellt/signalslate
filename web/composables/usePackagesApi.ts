// Source of truth: api/routers/packages.py (Pydantic Out/Body models). The request plumbing lives
// in utils/api/request.ts; a coded detail ({code, message}, e.g. package_exists,
// unrecognized_tracking_number, scan_failed) becomes ApiError.code.
import { request, seg } from '~/utils/api/request'

export type PackageStatus =
  | 'pending'
  | 'in_transit'
  | 'out_for_delivery'
  | 'delivered'
  | 'exception'
  | 'returned'
  | 'stale'
  | 'unknown'

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
