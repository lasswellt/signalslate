/**
 * The request plumbing every API composable shares: the CSRF header, JSON body, query cleanup
 * and error normalization. Nuxt does not auto-import from this nested path, so each composable
 * imports what it uses explicitly; `ApiError` is re-exported by useApi.ts for existing importers.
 */

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


export function isRecord(value: unknown): value is Record<string, unknown> {
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

export type Query = Record<string, string | number | boolean | undefined>

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  params?: Query
  // Only the OAuth start/paste/flow-status calls set this: the nonce cookie set by start must round-trip
  // cross-origin, and every other call is cookie-free so no ambient credential can ride along.
  credentials?: 'include'
}

/**
 * The one place a request is made, for every composable. Every non-GET carries X-Requested-With (the server's CSRF guard
 * rejects a write without it, body or not) and a JSON body, and every failure leaves as an ApiError.
 */
export async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
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

/** Encodes one path segment (an id, a source name) for interpolation into a URL. */
export const seg = encodeURIComponent
