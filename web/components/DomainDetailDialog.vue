<template>
  <DialogShell
    :model-value="open"
    :title="name"
    :subtitle="dialogSubtitle"
    width="640px"
    @update:model-value="onModelUpdate"
    @close="onDialogClose"
  >
    <div data-testid="domain-detail-dialog">
      <div v-if="loading" data-testid="domain-detail-loading">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="40%" />
        <q-skeleton type="text" width="80%" />
      </div>

      <div v-else-if="error" data-testid="domain-detail-error">
        <q-banner dense class="bg-negative text-white">{{ error }}</q-banner>
        <q-btn flat no-caps label="Retry" data-testid="domain-detail-retry" @click="load" />
      </div>

      <template v-else>
        <q-banner v-if="isAdHoc" dense class="bg-grey-3 q-mb-md" data-testid="domain-adhoc-note">
          This domain is not tracked. Showing a live lookup only.
        </q-banner>

        <div class="q-mb-md" data-testid="domain-summary">
          <div class="row items-center q-gutter-sm q-mb-xs">
            <StatusChip kind="health" :value="rdapStatus" />
            <span data-testid="domain-expiry">{{ expiryText }}</span>
          </div>
          <div class="text-body2 text-grey-8">Registrar: {{ rdapRegistrar ?? '—' }}</div>
          <div class="text-body2 text-grey-8">Nameservers: {{ nameserversText }}</div>
        </div>

        <q-list bordered class="rounded-borders">
          <q-expansion-item default-opened label="DNS records" data-testid="domain-dns">
            <q-card-section>
              <div v-if="dnsStatus !== 'ok'" class="text-grey-8" data-testid="domain-dns-unavailable">
                DNS records unavailable<template v-if="dnsError">: {{ dnsError }}</template>
              </div>
              <q-markup-table v-else dense flat data-testid="domain-dns-table">
                <thead>
                  <tr>
                    <th class="text-left">Type</th>
                    <th class="text-left">Values</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(record, rtype) in dnsRecords" :key="rtype">
                    <td>
                      {{ humanizeDnsType(rtype) }}
                      <q-tooltip>{{ rtype }}</q-tooltip>
                    </td>
                    <td>
                      <span v-if="!record.ok" class="text-negative">unavailable<template v-if="record.error">: {{ record.error }}</template></span>
                      <span v-else-if="record.values.length === 0" class="text-grey-8">none</span>
                      <span v-else>{{ record.values.join(', ') }}</span>
                    </td>
                  </tr>
                </tbody>
              </q-markup-table>
            </q-card-section>
          </q-expansion-item>

          <q-expansion-item default-opened label="Mail security" data-testid="domain-mail-posture">
            <q-card-section>
              <div v-if="mailStatus !== 'ok'" class="text-grey-8" data-testid="domain-mail-unavailable">
                Mail security unavailable<template v-if="mailError">: {{ mailError }}</template>
              </div>
              <template v-else>
                <q-banner v-if="isParked" dense class="bg-warning text-black" data-testid="domain-parked-warning">
                  Parked domain without a strict SPF/DMARC policy: mail can be spoofed for this domain.
                </q-banner>
                <div class="row q-gutter-sm">
                  <q-badge
                    v-for="badge in mailBadges"
                    :key="badge.key"
                    :color="badge.color"
                    :data-testid="`mail-badge-${badge.key}`"
                  >
                    {{ badge.label }}
                  </q-badge>
                </div>
              </template>
            </q-card-section>
          </q-expansion-item>

          <q-expansion-item label="Registration (RDAP)" data-testid="domain-rdap">
            <q-card-section>
              <div v-if="rdapStatus === 'unsupported'" class="text-grey-8" data-testid="domain-rdap-unsupported">
                RDAP is not supported for this TLD.
              </div>
              <div v-else-if="rdapStatus === 'not_found'" class="text-grey-8" data-testid="domain-rdap-not-found">
                No RDAP record found.
              </div>
              <div v-else-if="rdapStatus !== 'ok'" class="text-grey-8" data-testid="domain-rdap-unavailable">
                RDAP data unavailable<template v-if="rdapError">: {{ rdapError }}</template>
              </div>
              <div v-else data-testid="domain-rdap-details">
                <div>Registrar: {{ rdapRegistrar ?? '—' }}</div>
                <div>Created: {{ formatDate(rdapCreated) }}</div>
                <div>Expires: {{ formatDate(rdapExpires) }}</div>
                <div class="row items-center q-gutter-xs">
                  <span>Locked:</span>
                  <q-badge :color="rdapLocked ? 'positive' : 'negative'" data-testid="domain-rdap-locked">
                    {{ rdapLocked ? 'Yes' : 'No' }}
                  </q-badge>
                </div>
                <div v-if="rdapStatuses.length" class="row items-center q-gutter-xs q-mt-xs">
                  <span>Statuses:</span>
                  <q-chip v-for="status in rdapStatuses" :key="status" dense outline>
                    {{ humanizeRdapStatus(status) }}
                    <q-tooltip>{{ status }}</q-tooltip>
                  </q-chip>
                </div>
              </div>
            </q-card-section>
          </q-expansion-item>

          <q-expansion-item label="Subdomains" data-testid="domain-intel">
            <q-card-section>
              <q-btn
                v-if="!intel && !intelLoading"
                flat
                no-caps
                color="primary"
                label="Load subdomains & URLs"
                data-testid="domain-load-intel"
                @click="loadIntel"
              />
              <div v-if="intelLoading" class="row items-center q-gutter-sm" data-testid="domain-intel-loading">
                <q-spinner size="sm" color="primary" />
                <span>Loading subdomains and archived URLs&hellip;</span>
              </div>
              <q-banner v-if="intelError" dense class="bg-negative text-white" data-testid="domain-intel-error">
                {{ intelError }}
              </q-banner>
              <template v-if="intel">
                <div data-testid="domain-subdomains">
                  <div class="text-body2">Subdomains</div>
                  <div v-if="intel.subdomains.status !== 'ok'" class="text-grey-8" data-testid="domain-subdomains-unavailable">
                    Unavailable<template v-if="intel.subdomains.error">: {{ intel.subdomains.error }}</template>
                  </div>
                  <div v-else-if="subdomainNames.length === 0" class="text-grey-8">None found.</div>
                  <template v-else>
                    <ul>
                      <li v-for="sub in visibleSubdomains" :key="sub">{{ sub }}</li>
                    </ul>
                    <q-btn
                      v-if="subdomainNames.length > LIST_LIMIT"
                      flat
                      dense
                      no-caps
                      color="primary"
                      :label="subdomainsExpanded ? 'Show fewer' : `Show all ${subdomainNames.length}`"
                      data-testid="domain-subdomains-show-all"
                      @click="subdomainsExpanded = !subdomainsExpanded"
                    />
                  </template>
                </div>
                <div class="q-mt-md" data-testid="domain-archived-urls">
                  <div class="text-body2">Archived URLs</div>
                  <div v-if="intel.archived_urls.status !== 'ok'" class="text-grey-8" data-testid="domain-archived-urls-unavailable">
                    Unavailable<template v-if="intel.archived_urls.error">: {{ intel.archived_urls.error }}</template>
                  </div>
                  <div v-else-if="archivedUrls.length === 0" class="text-grey-8">None found.</div>
                  <template v-else>
                    <ul>
                      <li v-for="entry in visibleArchivedUrls" :key="entry">
                        <a :href="entry" target="_blank" rel="noopener">{{ entry }}</a>
                      </li>
                    </ul>
                    <q-btn
                      v-if="archivedUrls.length > LIST_LIMIT"
                      flat
                      dense
                      no-caps
                      color="primary"
                      :label="archivedExpanded ? 'Show fewer' : `Show all ${archivedUrls.length}`"
                      data-testid="domain-archived-urls-show-all"
                      @click="archivedExpanded = !archivedExpanded"
                    />
                  </template>
                </div>
              </template>
            </q-card-section>
          </q-expansion-item>

          <q-expansion-item v-if="!isAdHoc" label="Archive history" data-testid="domain-history">
            <q-card-section>
              <div v-if="history.length === 0" class="text-grey-8" data-testid="domain-history-empty">No snapshots yet.</div>
              <ul v-else>
                <li v-for="(entry, index) in history" :key="`${entry.taken_at ?? 'unknown'}-${index}`">
                  {{ entry.taken_at ? formatDate(entry.taken_at) : 'Unknown time' }}
                </li>
              </ul>
            </q-card-section>
          </q-expansion-item>
        </q-list>
      </template>
    </div>

    <template #actions>
      <q-btn flat no-caps icon="open_in_new" label="Open website" data-testid="domain-open-website" @click="openWebsite" />
      <q-btn flat no-caps label="Close" data-testid="domain-detail-close" @click="close" />
    </template>
  </DialogShell>
</template>

<script setup lang="ts">
import { ApiError } from '~/composables/useApi'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { InspectOut, SnapshotSummary } from '~/composables/useDomainsApi'
import DialogShell from '~/components/ui/DialogShell.vue'
import StatusChip from '~/components/ui/StatusChip.vue'

const props = defineProps<{
  open: boolean
  name: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  closed: []
}>()

const api = useDomainsApi()

const LIST_LIMIT = 10

// clientTransferProhibited etc. (RFC 7483 EPP status vocabulary): the handful that show up in
// practice get a plain-language label; anything else falls back to humanize(), with the raw
// value always available in a tooltip.
const RDAP_STATUS_LABELS: Record<string, string> = {
  clienttransferprohibited: 'Transfer locked',
  clientdeleteprohibited: 'Delete locked',
  clientupdateprohibited: 'Update locked',
  clienthold: 'On hold',
  clientrenewprohibited: 'Renewal locked',
  servertransferprohibited: 'Transfer locked (registry)',
  serverdeleteprohibited: 'Delete locked (registry)',
  serverupdateprohibited: 'Update locked (registry)',
  serverrenewprohibited: 'Renewal locked (registry)',
  serverhold: 'On hold (registry)',
  pendingdelete: 'Pending deletion',
  pendingtransfer: 'Pending transfer',
  pendingrenew: 'Pending renewal',
  redemptionperiod: 'Redemption period',
  autorenewperiod: 'Auto-renew grace period',
  active: 'Active',
  inactive: 'Inactive',
  ok: 'Active',
}

// Common DNS record types shown with a plain-language description; the raw type still appears in
// a tooltip. Anything not listed here (rare record types) is shown as-is.
const DNS_TYPE_LABELS: Record<string, string> = {
  a: 'IPv4 address (A)',
  aaaa: 'IPv6 address (AAAA)',
  mx: 'Mail exchange (MX)',
  txt: 'Text (TXT)',
  ns: 'Name server (NS)',
  cname: 'Alias (CNAME)',
  soa: 'Start of authority (SOA)',
  caa: 'Certificate authority (CAA)',
  srv: 'Service (SRV)',
}

function humanizeRdapStatus(status: string): string {
  return RDAP_STATUS_LABELS[status.toLowerCase()] ?? humanize(status)
}

function humanizeDnsType(rtype: string): string {
  return DNS_TYPE_LABELS[rtype.toLowerCase()] ?? rtype
}

interface DnsRecordView {
  ok: boolean
  values: string[]
  error: string | null
}

interface MailBadge {
  key: string
  label: string
  color: string
}

interface Snapshot {
  dns: unknown
  mail: unknown
  rdap: unknown
}

const loading = ref(false)
const error = ref<string | null>(null)
const isAdHoc = ref(false)
const history = ref<SnapshotSummary[]>([])
const snapshot = ref<Snapshot | null>(null)
const intel = ref<InspectOut['intel']>(null)
const intelLoading = ref(false)
const intelError = ref<string | null>(null)
const subdomainsExpanded = ref(false)
const archivedExpanded = ref(false)

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asBool(value: unknown): boolean {
  return value === true
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : []
}

const dnsSection = computed<Record<string, unknown> | null>(() => (isRecord(snapshot.value?.dns) ? snapshot.value.dns : null))
const dnsStatus = computed(() => asString(dnsSection.value?.status) ?? 'error')
const dnsError = computed(() => asString(dnsSection.value?.error))
const dnsRecords = computed<Record<string, DnsRecordView>>(() => {
  const records = dnsSection.value?.records
  if (!isRecord(records)) return {}
  const result: Record<string, DnsRecordView> = {}
  for (const [rtype, raw] of Object.entries(records)) {
    if (!isRecord(raw)) continue
    result[rtype] = { ok: asBool(raw.ok), values: asStringArray(raw.values), error: asString(raw.error) }
  }
  return result
})

const mailSection = computed<Record<string, unknown> | null>(() => (isRecord(snapshot.value?.mail) ? snapshot.value.mail : null))
const mailStatus = computed(() => asString(mailSection.value?.status) ?? 'error')
const mailError = computed(() => asString(mailSection.value?.error))
const isParked = computed(() => asStringArray(mailSection.value?.flags).some((flag) => flag.includes('parked')))
const mailBadges = computed<MailBadge[]>(() => {
  const mail = mailSection.value
  if (!mail) return []
  const spf = isRecord(mail.spf) ? mail.spf : null
  const dmarc = isRecord(mail.dmarc) ? mail.dmarc : null
  const dkimEntries = isRecord(mail.dkim) ? Object.values(mail.dkim) : []
  const dkimPresent = dkimEntries.some((entry) => isRecord(entry) && asBool(entry.present))
  const mtaSts = isRecord(mail.mta_sts) ? mail.mta_sts : null
  const bimi = isRecord(mail.bimi) ? mail.bimi : null
  const spfPresent = asBool(spf?.present)
  const dmarcPolicy = asString(dmarc?.policy)
  const mtaStsFetched = asBool(mtaSts?.policy_fetched)
  const bimiPresent = asBool(bimi?.present)
  return [
    { key: 'spf', label: spfPresent ? 'SPF present' : 'SPF missing', color: spfPresent ? 'positive' : 'negative' },
    {
      key: 'dmarc',
      label: dmarcPolicy ? `DMARC ${dmarcPolicy}` : 'DMARC missing',
      color: dmarcPolicy === 'reject' ? 'positive' : dmarcPolicy ? 'warning' : 'negative',
    },
    { key: 'dkim', label: dkimPresent ? 'DKIM present' : 'DKIM missing', color: dkimPresent ? 'positive' : 'negative' },
    { key: 'mta-sts', label: mtaStsFetched ? 'MTA-STS present' : 'MTA-STS missing', color: mtaStsFetched ? 'positive' : 'negative' },
    { key: 'bimi', label: bimiPresent ? 'BIMI present' : 'BIMI missing', color: bimiPresent ? 'positive' : 'negative' },
  ]
})

const rdapSection = computed<Record<string, unknown> | null>(() => (isRecord(snapshot.value?.rdap) ? snapshot.value.rdap : null))
const rdapStatus = computed(() => asString(rdapSection.value?.status) ?? 'error')
const rdapError = computed(() => asString(rdapSection.value?.error))
const rdapRegistrar = computed(() => asString(rdapSection.value?.registrar))
const rdapCreated = computed(() => asString(rdapSection.value?.created))
const rdapExpires = computed(() => asString(rdapSection.value?.expires))
const rdapLocked = computed(() => asBool(rdapSection.value?.locked))
const rdapStatuses = computed(() => asStringArray(rdapSection.value?.statuses))
const rdapNameservers = computed(() => asStringArray(rdapSection.value?.nameservers))
const nameserversText = computed(() => (rdapNameservers.value.length ? rdapNameservers.value.join(', ') : '—'))

const dialogSubtitle = computed(() => {
  if (loading.value || error.value) return undefined
  if (isAdHoc.value) return 'Not in your portfolio'
  return rdapRegistrar.value ?? undefined
})

const expiryText = computed(() => {
  if (loading.value || error.value) return ''
  if (!rdapExpires.value) return 'Expiry unknown'
  return `Expires ${formatDate(rdapExpires.value)} · ${relativeTime(rdapExpires.value)}`
})

const subdomainNames = computed(() => intel.value?.subdomains.names ?? [])
const archivedUrls = computed(() => intel.value?.archived_urls.urls ?? [])
const visibleSubdomains = computed(() => (subdomainsExpanded.value ? subdomainNames.value : subdomainNames.value.slice(0, LIST_LIMIT)))
const visibleArchivedUrls = computed(() => (archivedExpanded.value ? archivedUrls.value : archivedUrls.value.slice(0, LIST_LIMIT)))

/**
 * Loads the domain's stored detail (owned/watched domains). A 404 means name is not tracked (an
 * ad-hoc lookup), so this falls back to a live inspectDomain() call for DNS/mail/RDAP with no
 * snapshot history.
 */
async function load() {
  loading.value = true
  error.value = null
  isAdHoc.value = false
  history.value = []
  snapshot.value = null
  intel.value = null
  intelError.value = null
  subdomainsExpanded.value = false
  archivedExpanded.value = false
  try {
    const detail = await api.getDomain(props.name)
    history.value = detail.history
    const latest = detail.latest
    snapshot.value = {
      dns: isRecord(latest) ? latest.dns : null,
      mail: isRecord(latest) ? latest.mail : null,
      rdap: isRecord(latest) ? latest.rdap : null,
    }
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      isAdHoc.value = true
      try {
        const inspected = await api.inspectDomain({ name: props.name })
        snapshot.value = { dns: inspected.dns, mail: inspected.mail, rdap: inspected.rdap }
      } catch (inspectErr) {
        error.value = errorText(inspectErr)
      }
    } else {
      error.value = errorText(err)
    }
  } finally {
    loading.value = false
  }
}

/** On-demand crt.sh subdomains + Wayback archived-URL lookup: not fetched on open, only on request. */
async function loadIntel() {
  intelLoading.value = true
  intelError.value = null
  try {
    const result = await api.inspectDomain({ name: props.name, intel: true })
    intel.value = result.intel
    if (!result.intel) intelError.value = 'No subdomain or archived-URL data available'
  } catch (err) {
    intelError.value = errorText(err)
  } finally {
    intelLoading.value = false
  }
}

function openWebsite() {
  window.open(`https://${props.name}`, '_blank', 'noopener')
}

function close() {
  emit('update:open', false)
  emit('closed')
}

function onModelUpdate(value: boolean) {
  emit('update:open', value)
}

function onDialogClose() {
  emit('closed')
}

watch(
  () => [props.open, props.name] as const,
  ([isOpen]) => {
    if (isOpen) void load()
  },
  { immediate: true },
)
</script>
