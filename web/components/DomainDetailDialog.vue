<template>
  <!-- Not persistent: this dialog is read-only (no in-progress input to lose), so a click outside or
       Esc closes it the same as the Close button. -->
  <q-dialog :model-value="open" @update:model-value="onModelUpdate">
    <q-card style="min-width: 360px; max-width: 720px; width: 100%" data-testid="domain-detail-dialog">
      <q-card-section>
        <div class="text-h6" data-testid="dialog-title">{{ name }}</div>
      </q-card-section>

      <q-card-section v-if="loading" data-testid="domain-detail-loading">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="40%" />
        <q-skeleton type="text" width="80%" />
      </q-card-section>

      <q-card-section v-else-if="error" data-testid="domain-detail-error">
        <q-banner dense class="bg-negative text-white">{{ error }}</q-banner>
        <q-btn flat no-caps label="Retry" data-testid="domain-detail-retry" @click="load" />
      </q-card-section>

      <template v-else>
        <q-card-section v-if="isAdHoc" data-testid="domain-adhoc-note">
          <q-banner dense class="bg-grey-3">This domain is not tracked. Showing a live lookup only.</q-banner>
        </q-card-section>

        <q-card-section data-testid="domain-dns">
          <div class="text-subtitle2">DNS records</div>
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
                <td>{{ rtype }}</td>
                <td>
                  <span v-if="!record.ok" class="text-negative">unavailable<template v-if="record.error">: {{ record.error }}</template></span>
                  <span v-else-if="record.values.length === 0" class="text-grey-8">none</span>
                  <span v-else>{{ record.values.join(', ') }}</span>
                </td>
              </tr>
            </tbody>
          </q-markup-table>
        </q-card-section>

        <q-card-section data-testid="domain-mail-posture">
          <div class="text-subtitle2">Mail posture</div>
          <div v-if="mailStatus !== 'ok'" class="text-grey-8" data-testid="domain-mail-unavailable">
            Mail posture unavailable<template v-if="mailError">: {{ mailError }}</template>
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

        <q-card-section data-testid="domain-rdap">
          <div class="text-subtitle2">Registration (RDAP)</div>
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
            <div>Registrar: {{ rdapRegistrar ?? 'Unknown' }}</div>
            <div>Created: {{ formatDate(rdapCreated) }}</div>
            <div>Expires: {{ formatDate(rdapExpires) }}</div>
            <div class="row items-center q-gutter-xs">
              <span>Locked:</span>
              <q-badge :color="rdapLocked ? 'positive' : 'negative'" data-testid="domain-rdap-locked">
                {{ rdapLocked ? 'Yes' : 'No' }}
              </q-badge>
            </div>
            <div v-if="rdapStatuses.length">Statuses: {{ rdapStatuses.join(', ') }}</div>
          </div>
        </q-card-section>

        <q-card-section v-if="!isAdHoc" data-testid="domain-history">
          <div class="text-subtitle2">Snapshot history</div>
          <div v-if="history.length === 0" class="text-grey-8" data-testid="domain-history-empty">No snapshots yet.</div>
          <ul v-else>
            <li v-for="entry in history" :key="`${entry.taken_at ?? 'unknown'}-${entry.data_hash}`">
              {{ entry.taken_at ? formatDate(entry.taken_at) : 'unknown time' }} ({{ entry.data_hash.slice(0, 8) }})
            </li>
          </ul>
        </q-card-section>

        <q-card-section data-testid="domain-intel">
          <div class="text-subtitle2">Subdomains &amp; archived URLs</div>
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
              <div v-else-if="intel.subdomains.names.length === 0" class="text-grey-8">None found.</div>
              <ul v-else>
                <li v-for="sub in intel.subdomains.names" :key="sub">{{ sub }}</li>
              </ul>
            </div>
            <div data-testid="domain-archived-urls">
              <div class="text-body2">Archived URLs</div>
              <div v-if="intel.archived_urls.status !== 'ok'" class="text-grey-8" data-testid="domain-archived-urls-unavailable">
                Unavailable<template v-if="intel.archived_urls.error">: {{ intel.archived_urls.error }}</template>
              </div>
              <div v-else-if="intel.archived_urls.urls.length === 0" class="text-grey-8">None found.</div>
              <ul v-else>
                <li v-for="entry in intel.archived_urls.urls" :key="entry">{{ entry }}</li>
              </ul>
            </div>
          </template>
        </q-card-section>
      </template>

      <q-card-actions align="right">
        <q-btn flat no-caps label="Close" data-testid="domain-detail-close" @click="close" />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { ApiError, parseUtc } from '~/composables/useApi'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { InspectOut, SnapshotSummary } from '~/composables/useDomainsApi'

const props = defineProps<{
  open: boolean
  name: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  closed: []
}>()

const api = useDomainsApi()

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

function formatDate(value: string | null): string {
  if (!value) return 'Unknown'
  const date = parseUtc(value)
  return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleDateString()
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong'
}

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
        error.value = errorMessage(inspectErr)
      }
    } else {
      error.value = errorMessage(err)
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
    intelError.value = errorMessage(err)
  } finally {
    intelLoading.value = false
  }
}

function close() {
  emit('update:open', false)
  emit('closed')
}

function onModelUpdate(value: boolean) {
  emit('update:open', value)
  if (!value) emit('closed')
}

watch(
  () => [props.open, props.name] as const,
  ([isOpen]) => {
    if (isOpen) void load()
  },
  { immediate: true },
)
</script>
