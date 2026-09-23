<template>
  <div class="domain-table-scroll">
    <q-table
      :rows="rows"
      :columns="columns"
      row-key="name"
      flat
      bordered
      dense
      :loading="loading"
      :pagination="pagination"
      :data-testid="`${mode}-table`"
    >
      <template #body="rowProps">
        <q-tr
          :props="rowProps"
          class="cursor-pointer"
          :data-testid="`domain-row-${rowProps.row.name}`"
          @click="emit('open', rowProps.row)"
        >
          <q-td key="name" :props="rowProps">{{ rowProps.row.name }}</q-td>
          <q-td key="registrar" :props="rowProps">{{ sourceLabel(rowProps.row) }}</q-td>
          <q-td key="expiry" :props="rowProps">
            <div>{{ formatDate(rowProps.row.expires_at) }}</div>
            <q-badge v-if="expiryState(rowProps.row.expires_at).label" :color="expiryState(rowProps.row.expires_at).color">
              {{ expiryState(rowProps.row.expires_at).label }}
            </q-badge>
          </q-td>
          <q-td key="status" :props="rowProps">
            <div class="row q-gutter-xs items-center">
              <q-badge v-if="rowProps.row.missing_since" color="negative" :data-testid="`domain-status-${rowProps.row.name}`">
                Missing since {{ formatDate(rowProps.row.missing_since) }}
              </q-badge>
              <template v-else>
                <q-badge :color="rowProps.row.auto_renew ? 'positive' : 'grey-7'" :data-testid="`domain-status-${rowProps.row.name}`">
                  {{ rowProps.row.auto_renew ? 'Auto-renew on' : 'Auto-renew off' }}
                </q-badge>
                <q-badge :color="rowProps.row.locked ? 'positive' : 'grey-7'">
                  {{ rowProps.row.locked ? 'Locked' : 'Unlocked' }}
                </q-badge>
              </template>
            </div>
          </q-td>
          <q-td key="mail" :props="rowProps">
            <span v-if="rowProps.row.mail.status === 'unavailable'" class="text-grey-8">Not synced</span>
            <span v-else-if="rowProps.row.mail.status !== 'ok'" class="text-grey-8">Unavailable</span>
            <div v-else class="row q-gutter-xs">
              <q-badge
                v-for="badge in mailBadges(rowProps.row.mail)"
                :key="badge.key"
                :color="badge.color"
                :data-testid="`mail-badge-${rowProps.row.name}-${badge.key}`"
              >
                {{ badge.label }}
              </q-badge>
            </div>
          </q-td>
        </q-tr>
      </template>
    </q-table>
  </div>
</template>

<script setup lang="ts">
import { parseUtc } from '~/composables/useApi'
import type { DomainOut } from '~/composables/useDomainsApi'

/**
 * Shared portfolio/watchlist domain table (DESIGN.md density + status rules): sortable columns,
 * paginated 25 rows, expiry and status shown as text badges (never colour alone), and a single row
 * click opens the detail dialog instead of a duplicate "Inspect" button.
 */
interface Props {
  rows: DomainOut[]
  loading: boolean
  mode: 'portfolio' | 'watchlist'
}

defineProps<Props>()

const emit = defineEmits<{
  open: [row: DomainOut]
}>()

interface DomainColumn {
  name: string
  label: string
  field: string | ((row: DomainOut) => unknown)
  align: 'left' | 'center' | 'right'
  sortable?: boolean
}

const columns: DomainColumn[] = [
  { name: 'name', label: 'Domain', field: 'name', align: 'left', sortable: true },
  { name: 'registrar', label: 'Registrar', field: (row) => sourceLabel(row), align: 'left', sortable: true },
  { name: 'expiry', label: 'Expiry', field: (row) => expirySortValue(row.expires_at), align: 'left', sortable: true },
  { name: 'status', label: 'Status', field: (row) => statusSortValue(row), align: 'left', sortable: true },
  { name: 'mail', label: 'Mail', field: () => null, align: 'left' },
]

// Compact density per DESIGN.md: 25 rows per page instead of Quasar's ~5-row default.
const pagination = { rowsPerPage: 25 }

/**
 * Human label for the domain's source: the connection it was synced from when known (a raw
 * connection id, humanized), falling back to a humanized source name. Never shows a raw id.
 * @param row - The domain row.
 * @returns A plain-language registrar/source label.
 */
function sourceLabel(row: DomainOut): string {
  return row.connection_id ? humanize(row.connection_id) : humanize(row.source)
}

function daysUntil(value: string | null): number | null {
  if (!value) return null
  const target = parseUtc(value).getTime()
  if (Number.isNaN(target)) return null
  return Math.ceil((target - Date.now()) / 86_400_000)
}

// Sorting needs a comparable number: unknown expiry sorts last (Infinity), not first.
function expirySortValue(value: string | null): number {
  const days = daysUntil(value)
  return days === null ? Infinity : days
}

interface ExpiryState {
  label: string
  color: string
}

/**
 * Expiry state as a text chip (DESIGN.md: status never colour alone): "Expired" when past due,
 * "Expires in N days" inside the 30-day warning window, otherwise no chip (date alone is enough).
 * @param value - The domain's expires_at ISO timestamp, or null when unknown.
 * @returns The chip label/color, or an empty label when no chip is warranted.
 */
function expiryState(value: string | null): ExpiryState {
  const days = daysUntil(value)
  if (days === null) return { label: '', color: '' }
  if (days < 0) return { label: 'Expired', color: 'negative' }
  if (days <= 30) return { label: `Expires in ${days} day${days === 1 ? '' : 's'}`, color: 'warning' }
  return { label: '', color: '' }
}

// Missing ranks worst, then not-auto-renewing, then locked-but-renewing, then fully healthy — a
// stable, low-to-high ordering for the sortable Status column.
function statusSortValue(row: DomainOut): number {
  if (row.missing_since) return 0
  if (!row.auto_renew) return 1
  if (!row.locked) return 2
  return 3
}

interface MailBadge {
  key: string
  label: string
  color: string
}

/**
 * Per-record mail-posture badges: text conveys the state (never colour alone). DMARC has three
 * states (enforced/weak/missing) matching DomainDetailDialog's mailBadges semantics; the rest are
 * present/missing.
 * @param mail - The domain's mail posture summary.
 * @returns One badge per mail control.
 */
function mailBadges(mail: DomainOut['mail']): MailBadge[] {
  const dmarc: MailBadge = mail.dmarc_policy === 'reject'
    ? { key: 'dmarc', label: 'DMARC enforced', color: 'positive' }
    : mail.dmarc_policy
      ? { key: 'dmarc', label: 'DMARC weak', color: 'warning' }
      : { key: 'dmarc', label: 'DMARC missing', color: 'negative' }
  return [
    { key: 'spf', label: mail.spf ? 'SPF present' : 'SPF missing', color: mail.spf ? 'positive' : 'negative' },
    dmarc,
    { key: 'dkim', label: mail.dkim ? 'DKIM present' : 'DKIM missing', color: mail.dkim ? 'positive' : 'negative' },
    { key: 'mta-sts', label: mail.mta_sts ? 'MTA-STS present' : 'MTA-STS missing', color: mail.mta_sts ? 'positive' : 'negative' },
    { key: 'bimi', label: mail.bimi ? 'BIMI present' : 'BIMI missing', color: mail.bimi ? 'positive' : 'negative' },
  ]
}
</script>

<style scoped lang="scss">
.domain-table-scroll {
  overflow-x: auto;
}
</style>
