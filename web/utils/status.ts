/**
 * Central status-to-{color,icon,label} mapping. One place decides what a status "means" visually,
 * so a fix (e.g. partial should never read as an error) lands everywhere at once instead of being
 * re-derived per page. Colors are Quasar palette names (positive/warning/negative/info/grey);
 * icons are Material Symbols names used with <q-icon>/<q-badge>.
 */

/** The families of status this app renders. Extend when a new domain of statuses appears. */
export type StatusKind =
  | 'run'
  | 'health'
  | 'purchase'
  | 'application'
  | 'assist'
  | 'confidence'
  | 'fit'
  | 'domain'

export interface StatusMeta {
  color: string
  icon: string
  label: string
}

const UNKNOWN: StatusMeta = { color: 'grey', icon: 'help', label: 'Unknown' }

/** Turns `likely_available` / `not_ready` into `Likely available` / `Not ready`; `ok` stays `OK`. */
function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/g, ' ').trim()
  if (!spaced) return 'Unknown'
  if (spaced.toLowerCase() === 'ok') return 'OK'
  return spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase()
}

// run/health: the same vocabulary describes a pipeline Run's status and a per-source health/attempt
// status. `partial` must read as warning, never as an error (it used to show red on the collectors
// page) — see the plan note this task was scoped from.
const RUN_HEALTH_COLOR: Record<string, string> = {
  ok: 'positive',
  success: 'positive',
  partial: 'warning',
  running: 'info',
  error: 'negative',
  failed: 'negative',
  skipped: 'grey',
}
const RUN_HEALTH_ICON: Record<string, string> = {
  ok: 'check_circle',
  success: 'check_circle',
  partial: 'warning',
  running: 'autorenew',
  error: 'error',
  failed: 'error',
  skipped: 'remove_circle_outline',
}

function runHealthMeta(value: string): StatusMeta {
  const key = value.toLowerCase()
  const color = RUN_HEALTH_COLOR[key]
  if (!color) return { ...UNKNOWN, label: humanize(value) }
  return { color, icon: RUN_HEALTH_ICON[key], label: humanize(value) }
}

// Domain purchase status. `domains.vue`'s purchaseStatusColor only ever compared against
// submitted/confirmed/failed/refused; pipeline/domains/purchase.py actually writes
// pending/succeeded/failed/unknown. Both vocabularies are covered so real API values render sensibly.
function purchaseMeta(value: string): StatusMeta {
  const key = value.toLowerCase()
  if (key === 'submitted' || key === 'confirmed' || key === 'succeeded') {
    return { color: 'positive', icon: 'check_circle', label: humanize(value) }
  }
  if (key === 'failed' || key === 'refused') {
    return { color: 'negative', icon: 'error', label: humanize(value) }
  }
  if (key === 'pending') {
    return { color: 'warning', icon: 'schedule', label: humanize(value) }
  }
  if (key === 'unknown') {
    return { color: 'grey', icon: 'help', label: 'Unknown' }
  }
  return { color: 'grey', icon: 'help', label: humanize(value) }
}

// Job application status (web/pages/jobs.vue applicationStatusColor).
function applicationMeta(value: string): StatusMeta {
  const key = value.toLowerCase()
  if (key === 'submitted' || key === 'interviewing') {
    return { color: 'positive', icon: 'check_circle', label: humanize(value) }
  }
  if (key === 'rejected' || key === 'withdrawn') {
    return { color: 'negative', icon: 'error', label: humanize(value) }
  }
  return { color: 'grey', icon: 'schedule', label: humanize(value) }
}

// Desktop-assist application state (api/routers/job_apply.py _ASSIST_STATES).
const ASSIST_COLOR: Record<string, string> = {
  idle: 'grey',
  queued: 'info',
  claimed: 'info',
  running: 'info',
  paused: 'warning',
  done: 'positive',
  failed: 'negative',
}
const ASSIST_ICON: Record<string, string> = {
  idle: 'radio_button_unchecked',
  queued: 'schedule',
  claimed: 'autorenew',
  running: 'autorenew',
  paused: 'pause_circle',
  done: 'check_circle',
  failed: 'error',
}

function assistMeta(value: string): StatusMeta {
  const key = value.toLowerCase()
  const color = ASSIST_COLOR[key]
  if (!color) return { ...UNKNOWN, label: humanize(value) }
  return { color, icon: ASSIST_ICON[key], label: humanize(value) }
}

// Domain idea availability (web/components/DomainIdeasPanel.vue statusColor/statusLabel).
function domainMeta(value: string): StatusMeta {
  const key = value.toLowerCase()
  if (key === 'likely_available') return { color: 'positive', icon: 'check_circle', label: 'Likely available' }
  if (key === 'taken') return { color: 'negative', icon: 'cancel', label: 'Taken' }
  return { color: 'grey', icon: 'help', label: 'Unknown' }
}

/** Clamps a possibly-out-of-range fraction/score into [0, 1] before scaling to a percent label. */
function toPercentLabel(fraction: number): string {
  const pct = Math.round(Math.max(0, Math.min(1, fraction)) * 100)
  return `${pct}%`
}

// Job-board resolution confidence, 0-1 (web/components/JobCompaniesPanel.vue confidenceColor).
function confidenceMeta(value: number): StatusMeta {
  if (Number.isNaN(value)) return { ...UNKNOWN, label: 'Unknown' }
  if (value >= 0.75) return { color: 'positive', icon: 'check_circle', label: toPercentLabel(value) }
  if (value >= 0.4) return { color: 'warning', icon: 'warning', label: toPercentLabel(value) }
  return { color: 'negative', icon: 'error', label: toPercentLabel(value) }
}

// Posting fit score, 0-100 (web/components/JobDetailDialog.vue fitColor); null/absent means unscored.
function fitMeta(value: number | null): StatusMeta {
  if (value === null || Number.isNaN(value)) return { color: 'grey', icon: 'help', label: 'Unscored' }
  const label = String(Math.round(value))
  if (value >= 75) return { color: 'positive', icon: 'check_circle', label }
  if (value >= 50) return { color: 'warning', icon: 'warning', label }
  return { color: 'negative', icon: 'error', label }
}

/** Normalized mail-posture flags shared by DomainTable and DomainDetailDialog; each adapts its own API shape into this. */
export interface MailPosture {
  spf: boolean
  dkim: boolean
  /** Raw DMARC policy (reject/quarantine/none), or null when no DMARC record exists. */
  dmarcPolicy: string | null
  mtaSts: boolean
  bimi: boolean
}

export interface MailBadge {
  key: string
  label: string
  color: string
  icon: string
}

/**
 * Shared SPF/DMARC/DKIM/MTA-STS/BIMI badges. DMARC has three states (enforced/weak/missing); the
 * rest are present/missing. `dmarcPolicyLabel` preserves each caller's original wording: false gives
 * DomainTable's generic "DMARC enforced"/"DMARC weak"/"DMARC missing"; true gives
 * DomainDetailDialog's literal "DMARC <policy>"/"DMARC missing".
 * @param mail - Normalized mail-posture flags.
 * @param dmarcPolicyLabel - When true, a non-missing DMARC label shows the raw policy value.
 * @returns One badge per mail control, in SPF/DMARC/DKIM/MTA-STS/BIMI order.
 */
export function mailBadges(mail: MailPosture, dmarcPolicyLabel = false): MailBadge[] {
  const dmarcColor = mail.dmarcPolicy === 'reject' ? 'positive' : mail.dmarcPolicy ? 'warning' : 'negative'
  const dmarcLabel = mail.dmarcPolicy === null
    ? 'DMARC missing'
    : dmarcPolicyLabel
      ? `DMARC ${mail.dmarcPolicy}`
      : mail.dmarcPolicy === 'reject' ? 'DMARC enforced' : 'DMARC weak'
  const dmarcIcon = mail.dmarcPolicy === 'reject' ? 'check_circle' : mail.dmarcPolicy ? 'warning' : 'error'
  return [
    { key: 'spf', label: mail.spf ? 'SPF present' : 'SPF missing', color: mail.spf ? 'positive' : 'negative', icon: mail.spf ? 'check_circle' : 'error' },
    { key: 'dmarc', label: dmarcLabel, color: dmarcColor, icon: dmarcIcon },
    { key: 'dkim', label: mail.dkim ? 'DKIM present' : 'DKIM missing', color: mail.dkim ? 'positive' : 'negative', icon: mail.dkim ? 'check_circle' : 'error' },
    { key: 'mta-sts', label: mail.mtaSts ? 'MTA-STS present' : 'MTA-STS missing', color: mail.mtaSts ? 'positive' : 'negative', icon: mail.mtaSts ? 'check_circle' : 'error' },
    { key: 'bimi', label: mail.bimi ? 'BIMI present' : 'BIMI missing', color: mail.bimi ? 'positive' : 'negative', icon: mail.bimi ? 'check_circle' : 'error' },
  ]
}

/**
 * Maps a status kind + raw value to a Quasar color, an icon name and a human label.
 * @param kind - Which status vocabulary `value` belongs to.
 * @param value - The raw status string, or a number for the numeric kinds (`confidence`, `fit`).
 * @returns `{color, icon, label}`; unrecognized string values become grey + help icon + a humanized label.
 */
export function statusMeta(kind: StatusKind, value: string | number | null | undefined): StatusMeta {
  if (kind === 'confidence') {
    return confidenceMeta(typeof value === 'number' ? value : NaN)
  }
  if (kind === 'fit') {
    return fitMeta(typeof value === 'number' ? value : null)
  }

  if (value === null || value === undefined) return UNKNOWN
  const text = String(value)
  if (text.trim() === '') return UNKNOWN

  switch (kind) {
    case 'run':
    case 'health':
      return runHealthMeta(text)
    case 'purchase':
      return purchaseMeta(text)
    case 'application':
      return applicationMeta(text)
    case 'assist':
      return assistMeta(text)
    case 'domain':
      return domainMeta(text)
    default:
      return { ...UNKNOWN, label: humanize(text) }
  }
}
