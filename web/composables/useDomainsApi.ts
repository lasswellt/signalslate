// Mirrors api/routers/domains.py and api/routers/domain_buy.py. Split from useApi.ts to keep that
// file from growing; the request plumbing lives in utils/api/request.ts.
import { request, seg } from '~/utils/api/request'

// api/routers/domains.py MailSummaryOut: a presence-only summary of the domain's latest stored
// mail-posture snapshot. status "unavailable" means no snapshot has been taken yet (never synced).
export interface MailSummaryOut {
  status: string // "ok" | "error" | "unavailable"
  spf: boolean
  dmarc_policy: string | null // null: no DMARC record; otherwise the published policy (reject/quarantine/none)
  dkim: boolean
  mta_sts: boolean
  bimi: boolean
}

export interface DomainOut {
  name: string
  ownership: string
  source: string
  connection_id: string | null
  expires_at: string | null
  auto_renew: boolean | null
  locked: boolean | null
  privacy: boolean | null
  first_seen: string | null
  last_seen: string | null
  missing_since: string | null
  mail: MailSummaryOut
}

export interface SnapshotSummary {
  taken_at: string | null
  data_hash: string
}

export interface DomainDetailOut extends DomainOut {
  latest: Record<string, unknown> | null
  latest_taken_at: string | null
  history: SnapshotSummary[]
}

export interface AddDomainBody {
  name: string
  ownership?: 'owned' | 'watched'
}

export interface ImportDomainsBody {
  csv: string
}

export interface RejectedRow {
  line: number
  reason: string
}

export interface ImportOut {
  added: string[]
  rejected: RejectedRow[]
}

export interface SyncStatusOut {
  connection_id: string
  kind: string
  status: string
  detail: string
  domain_count: number
}

export interface SnapshotStatusOut {
  name: string
  status: string
  detail: string
}

export interface SyncOut {
  sync: SyncStatusOut[]
  refresh: SnapshotStatusOut[]
}

export interface EgressIpOut {
  // "unknown" (never absent) on lookup failure — mirrors api/routers/domains.py's EgressIpOut.
  ip: string
}

export interface InspectBody {
  name: string
  intel?: boolean
}

export interface InspectOut {
  name: string
  dns: Record<string, unknown>
  mail: Record<string, unknown>
  rdap: Record<string, unknown>
  data_hash: string
  intel: {
    subdomains: { status: string; names: string[]; error: string | null }
    archived_urls: { status: string; urls: string[]; error: string | null }
  } | null
}

export interface IdeasBody {
  seeds: string[]
  tlds: string[]
  brief?: string | null
  use_llm?: boolean
}

export interface CandidateOut {
  name: string
  status: string
}

export interface IdeasOut {
  candidates: CandidateOut[]
  llm_reason: string | null
}

export interface CheckDomainsBody {
  names: string[]
  connection_id: string
}

export interface QuoteOut {
  name: string
  available: boolean
  premium: boolean
  price: string
  renewal_price: string | null
  currency: string
}

export interface CreateQuoteBody {
  name: string
  connection_id: string
}

export interface StoredQuoteOut {
  id: string
  name: string
  connection_id: string
  price: string
  renewal_price: string | null
  currency: string
  premium: boolean
  expires_at: string | null
}

// api/routers/domain_buy.py execute_purchase()/PurchaseRefused.reason_code (pipeline/domains/purchase.py).
export type PurchaseRefusedReason =
  | 'missing_registrant_contact'
  | 'purchase_disabled'
  | 'quote_not_found'
  | 'quote_expired'
  | 'name_mismatch'
  | 'premium_blocked'
  | 'invalid_years'
  | 'invalid_price'
  | 'price_exceeds_cap'
  | 'daily_cap_exceeded'
  | 'registrar_unavailable'
  | 'already_submitted'

export interface PurchaseBody {
  quote_id: string
  confirm_name: string
  years?: number
}

export interface PurchaseOut {
  id: number
  quote_id: string
  name?: string | null
  currency?: string | null
  status: string
  price: string
  created_at: string | null
  detail: string | null
}

export interface PurchaseSettingsOut {
  enabled: boolean
  max_price: string
  daily_cap: string
  allow_premium: boolean
  remaining_today: string
}

/**
 * Typed client for the domain portfolio and domain-buying routes (api/routers/domains.py,
 * api/routers/domain_buy.py). Holds no state.
 * @returns The API methods.
 * @throws ApiError from any method when the request fails.
 */
export function useDomainsApi() {
  const { public: { apiBase } } = useRuntimeConfig()
  const url = (path: string) => `${apiBase}/api${path}`

  return {
    listDomains: (options: { ownership?: string; source?: string } = {}) =>
      request<DomainOut[]>(url('/domains'), { params: { ownership: options.ownership, source: options.source } }),
    // For prefilling a Namecheap connection's client_ip field: that address is not something
    // Namecheap hands out, so the dialog offers to detect it instead.
    getEgressIp: () => request<EgressIpOut>(url('/domains/egress-ip')),
    getDomain: (name: string) => request<DomainDetailOut>(url(`/domains/${seg(name)}`)),
    addDomain: (payload: AddDomainBody) => request<DomainOut>(url('/domains'), { method: 'POST', body: payload }),
    deleteDomain: (name: string) => request<void>(url(`/domains/${seg(name)}`), { method: 'DELETE' }),
    importDomains: (payload: ImportDomainsBody) =>
      request<ImportOut>(url('/domains/import'), { method: 'POST', body: payload }),
    syncDomains: () => request<SyncOut>(url('/domains/sync'), { method: 'POST' }),
    inspectDomain: (payload: InspectBody) =>
      request<InspectOut>(url('/domains/inspect'), { method: 'POST', body: payload }),

    generateIdeas: (payload: IdeasBody) => request<IdeasOut>(url('/domains/ideas'), { method: 'POST', body: payload }),
    checkDomains: (payload: CheckDomainsBody) =>
      request<QuoteOut[]>(url('/domains/check'), { method: 'POST', body: payload }),
    createQuote: (payload: CreateQuoteBody) =>
      request<StoredQuoteOut>(url('/domains/quotes'), { method: 'POST', body: payload }),
    // POST /api/domains/purchases
    createPurchase: (payload: PurchaseBody) =>
      request<PurchaseOut>(url('/domains/purchases'), { method: 'POST', body: payload }),
    // GET /api/domains/purchases
    listPurchases: () => request<PurchaseOut[]>(url('/domains/purchases')),
    getPurchaseSettings: () => request<PurchaseSettingsOut>(url('/domains/purchase-settings')),
  }
}
