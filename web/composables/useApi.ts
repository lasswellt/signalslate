export interface SourceHealth {
  source: string
  status: 'ok' | 'error'
  detail: string
  checked_at: string
}

export interface RunSummary {
  id: number
  trigger: 'manual' | 'scheduled'
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

export function useApi() {
  const { public: { apiBase } } = useRuntimeConfig()

  return {
    getStatus: () => $fetch<StatusResponse>(`${apiBase}/api/status`),
    getRuns: (limit = 50) => $fetch<RunSummary[]>(`${apiBase}/api/runs`, { params: { limit } }),
    getRun: (id: number) => $fetch<RunDetail>(`${apiBase}/api/runs/${id}`),
    triggerRun: () => $fetch<{ accepted: boolean }>(`${apiBase}/api/runs/trigger`, { method: 'POST' }),
    getConfig: () => $fetch<DigestConfig>(`${apiBase}/api/config`),
    updateConfig: (payload: DigestConfig) =>
      $fetch<DigestConfig>(`${apiBase}/api/config`, { method: 'PUT', body: payload }),
    pdfUrl: (id: number) => `${apiBase}/api/runs/${id}/pdf`,
  }
}
