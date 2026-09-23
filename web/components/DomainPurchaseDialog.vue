<template>
  <!-- Not persistent: a click outside or Esc is just Cancel here, same as the Cancel button; nothing
       typed survives a reopen (reset() runs every time the dialog opens). -->
  <div data-testid="domain-purchase-dialog">
    <DialogShell
      :model-value="open"
      :title="dialogTitle"
      :busy="submitting"
      @update:model-value="(value: boolean) => emit('update:open', value)"
      @close="() => emit('closed')"
    >
      <q-card-section v-if="loading" data-testid="purchase-loading">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="40%" />
        <q-skeleton type="text" width="30%" />
      </q-card-section>

      <q-card-section v-else-if="loadError" data-testid="purchase-load-error">
        <q-banner dense class="bg-negative text-white">{{ loadError }}</q-banner>
        <q-btn flat no-caps label="Retry" data-testid="purchase-retry" @click="load" />
      </q-card-section>

      <q-card-section v-else-if="result" data-testid="purchase-result">
        <q-banner dense :class="resultBannerClass">{{ resultMessage }}</q-banner>
      </q-card-section>

      <q-card-section v-else-if="quote && settings" class="q-gutter-sm" data-testid="purchase-quote">
        <q-banner v-if="!settings.enabled" dense class="bg-warning text-black" data-testid="purchase-disabled-note">
          Purchasing is turned off on this server. An administrator can enable it in the server settings.
        </q-banner>

        <div data-testid="purchase-registrar">Registrar connection: {{ registrarLabel }}</div>

        <div class="row items-center q-gutter-xs">
          <span data-testid="purchase-first-year-price">First year: {{ formatMoney(Number(quote.price), quote.currency) }}</span>
          <q-badge v-if="quote.premium" color="warning" text-color="black" data-testid="purchase-premium-badge">Premium</q-badge>
        </div>
        <div data-testid="purchase-renewal-price">Renewal: {{ formatMoney(Number(quote.renewal_price ?? quote.price), quote.currency) }}/yr</div>
        <div data-testid="purchase-remaining-cap">Spending limit left today: {{ formatMoney(Number(settings.remaining_today), quote.currency) }}</div>

        <div v-if="!isExpired" data-testid="purchase-countdown">Quote expires in {{ countdownText }}</div>
        <div v-else class="row items-center q-gutter-sm" data-testid="purchase-expired">
          <span>Quote expired.</span>
          <q-btn flat dense no-caps color="primary" label="Get new quote" :loading="loading" data-testid="purchase-get-new-quote" @click="load" />
        </div>

        <q-select
          v-model="years"
          :options="YEAR_OPTIONS"
          emit-value
          map-options
          label="Years"
          outlined
          dense
          :disable="submitting"
          data-testid="purchase-years"
        />
        <div data-testid="purchase-total">Total for {{ years }} year{{ years === 1 ? '' : 's' }}: {{ formatMoney(totalPrice, quote.currency) }}</div>

        <q-input
          v-model="confirmName"
          label="Type the domain name to confirm"
          outlined
          dense
          autocomplete="off"
          :disable="submitting"
          data-testid="purchase-confirm-name"
        />

        <q-banner v-if="submitError" dense class="bg-negative text-white" data-testid="purchase-error">
          {{ submitError }}
        </q-banner>
      </q-card-section>

      <template #actions>
        <template v-if="result">
          <q-btn flat no-caps label="Close" data-testid="purchase-close" @click="requestCloseNow" />
        </template>
        <template v-else-if="quote && settings">
          <q-btn flat no-caps label="Cancel" :disable="submitting" data-testid="purchase-cancel" @click="requestCloseNow" />
          <q-btn
            color="primary"
            no-caps
            label="Buy"
            :loading="submitting"
            :disable="!canBuy"
            data-testid="purchase-buy"
            @click="onBuy"
          />
        </template>
      </template>
    </DialogShell>
  </div>
</template>

<script setup lang="ts">
import { ApiError, parseUtc } from '~/composables/useApi'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { PurchaseOut, PurchaseRefusedReason, PurchaseSettingsOut, StoredQuoteOut } from '~/composables/useDomainsApi'
import DialogShell from '~/components/ui/DialogShell.vue'

const props = defineProps<{
  open: boolean
  name: string
  connectionId: string
  /** Human label for the registrar connection (e.g. "Main GoDaddy (godaddy)"), when the caller has
   * one on hand. Falls back to a humanized connectionId so this dialog never shows nothing. */
  connectionLabel?: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  purchased: [purchase: PurchaseOut]
  closed: []
}>()

const api = useDomainsApi()

const dialogTitle = computed(() => `Buy ${props.name}`)
const registrarLabel = computed(() => props.connectionLabel || humanize(props.connectionId))

const YEAR_OPTIONS = Array.from({ length: 10 }, (_, index) => ({
  label: `${index + 1} year${index === 0 ? '' : 's'}`,
  value: index + 1,
}))

// api/routers/domain_buy.py create_purchase() returns PurchaseRefused.reason_code (pipeline/domains/
// purchase.py) as-is as the 422 `code`; every value here mirrors PurchaseRefusedReason exactly.
const REASON_LABELS: Record<PurchaseRefusedReason, string> = {
  missing_registrant_contact: 'No registrant contact is on file for this connection.',
  purchase_disabled: 'Domain purchasing is disabled on this server. An administrator can enable it in the server settings.',
  quote_not_found: 'This quote could not be found; get a new one.',
  quote_expired: 'This quote has expired; get a new one.',
  name_mismatch: 'The typed name does not match the quoted domain.',
  premium_blocked: 'Premium domains are disabled on this server.',
  invalid_years: 'Years must be between 1 and 10.',
  invalid_price: 'The quote has an invalid price.',
  price_exceeds_cap: 'The total price exceeds the configured maximum price.',
  daily_cap_exceeded: "This purchase would exceed today's daily spending cap.",
  registrar_unavailable: 'The registrar connection for this purchase is unavailable.',
  already_submitted: 'A purchase for this quote was already submitted.',
}

function isReasonCode(value: string | undefined): value is PurchaseRefusedReason {
  return !!value && value in REASON_LABELS
}

const loading = ref(false)
const loadError = ref<string | null>(null)
const settings = ref<PurchaseSettingsOut | null>(null)
const quote = ref<StoredQuoteOut | null>(null)
const confirmName = ref('')
const years = ref(1)
const submitting = ref(false)
const submitError = ref<string | null>(null)
const result = ref<PurchaseOut | null>(null)
const nowMs = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

function errorMessage(err: unknown): string {
  if (err instanceof ApiError && isReasonCode(err.code)) return REASON_LABELS[err.code]
  return errorText(err)
}

/** Loads purchase settings and issues a fresh quote; run on open and again for "Get new quote". */
async function load() {
  loading.value = true
  loadError.value = null
  try {
    const [loadedSettings, loadedQuote] = await Promise.all([
      api.getPurchaseSettings(),
      api.createQuote({ name: props.name, connection_id: props.connectionId }),
    ])
    settings.value = loadedSettings
    quote.value = loadedQuote
  } catch (err) {
    loadError.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

const expiresAtMs = computed(() => {
  if (!quote.value?.expires_at) return null
  const parsed = parseUtc(quote.value.expires_at)
  return Number.isNaN(parsed.getTime()) ? null : parsed.getTime()
})

const isExpired = computed(() => expiresAtMs.value !== null && expiresAtMs.value <= nowMs.value)

const countdownText = computed(() => {
  if (expiresAtMs.value === null) return ''
  const remaining = Math.max(0, expiresAtMs.value - nowMs.value)
  const minutes = Math.floor(remaining / 60000)
  const seconds = Math.floor((remaining % 60000) / 1000)
  return `${minutes}:${String(seconds).padStart(2, '0')}`
})

const renewalUnitPrice = computed(() => Number(quote.value?.renewal_price ?? quote.value?.price ?? '0'))

// Mirrors pipeline.domains.purchase.execute_purchase()'s own `total` (price + (years - 1) * renewal,
// or price when the registrar didn't quote a renewal): the server enforces max_price/daily_cap
// against this exact total, so this display must compute it the same way.
const totalPrice = computed(() => {
  if (!quote.value) return 0
  return Number(quote.value.price) + (years.value - 1) * renewalUnitPrice.value
})

const nameMatches = computed(() => quote.value !== null && confirmName.value.trim() === quote.value.name)

const canBuy = computed(
  () => !!settings.value?.enabled && !!quote.value && !isExpired.value && nameMatches.value && !submitting.value,
)

const resultMessage = computed(() => {
  const purchase = result.value
  if (!purchase) return ''
  const domainName = quote.value?.name ?? props.name
  if (purchase.status === 'succeeded') return `${domainName} purchased.${purchase.detail ? ` ${purchase.detail}` : ''}`
  if (purchase.status === 'unknown') {
    return `Purchase outcome unknown: the registrar call did not confirm success or failure. This will be reconciled on the next sync.${purchase.detail ? ` ${purchase.detail}` : ''}`
  }
  if (purchase.status === 'failed') return `Purchase failed.${purchase.detail ? ` ${purchase.detail}` : ''}`
  return purchase.detail ?? purchase.status
})

const resultBannerClass = computed(() => {
  const status = result.value?.status
  if (status === 'succeeded') return 'bg-positive text-white'
  if (status === 'failed') return 'bg-negative text-white'
  return 'bg-warning text-black' // "unknown" and any other status: not a hard failure, needs follow-up
})

/** Buys the quoted domain. Guarded against a second in-flight call by the same check that also
 * drives the Buy button's :disable, so a click that slips past a stale disabled render still no-ops. */
async function onBuy() {
  if (submitting.value || !canBuy.value || !quote.value) return
  submitting.value = true
  submitError.value = null
  try {
    const purchase = await api.createPurchase({
      quote_id: quote.value.id,
      confirm_name: confirmName.value.trim(),
      years: years.value,
    })
    result.value = purchase
    emit('purchased', purchase)
  } catch (err) {
    submitError.value = errorMessage(err)
  } finally {
    submitting.value = false
  }
}

function reset() {
  loadError.value = null
  settings.value = null
  quote.value = null
  confirmName.value = ''
  years.value = 1
  submitting.value = false
  submitError.value = null
  result.value = null
}

/** Cancel/Close button handler: closes immediately, bypassing DialogShell's own busy/dirty guard
 * (these buttons already gate themselves with :disable). */
function requestCloseNow() {
  emit('update:open', false)
  emit('closed')
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      reset()
      void load()
    }
  },
  { immediate: true },
)

onMounted(() => {
  timer = setInterval(() => {
    nowMs.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
