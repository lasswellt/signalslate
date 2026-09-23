import { ApiError } from '~/composables/useApi'

/**
 * Renders any caught value as a user-safe message. Consolidates the `errorText` copies duplicated
 * across pages (e.g. connections.vue, collectors.vue): `ApiError.message` already carries the
 * server's `detail` (a plain string, or a FastAPI validation array joined into one line by
 * `toApiError` in useApi.ts), so an `ApiError` only needs its `message` read back out.
 * @param e - The caught value; typically an `ApiError`, another `Error`, or a plain string.
 * @param fallback - Message to use when `e` carries no usable text.
 * @returns A message safe to show a user.
 */
export function errorText(e: unknown, fallback = 'Something went wrong'): string {
  if (e instanceof ApiError) return e.message || fallback
  if (e instanceof Error) return e.message || fallback
  if (typeof e === 'string') return e || fallback
  return fallback
}
