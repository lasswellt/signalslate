<template>
  <DialogShell
    :model-value="modelValue"
    title="Add package"
    subtitle="Carrier is detected from the tracking number"
    width="520px"
    :busy="pending"
    @update:model-value="onModelUpdate"
  >
    <div data-testid="package-add-dialog">
      <div v-if="result" data-testid="package-add-result">
        <div class="text-subtitle2">Package added</div>
        <div class="q-my-sm">
          <div>Carrier: <span class="text-weight-medium" data-testid="package-add-result-carrier">{{ carrierLabel(result.carrier) }}</span></div>
          <div>Tracking number: <span class="text-weight-medium" data-testid="package-add-result-number">{{ result.tracking_number }}</span></div>
        </div>
        <a
          v-if="result.tracking_url"
          :href="result.tracking_url"
          target="_blank"
          rel="noopener noreferrer"
          data-testid="package-add-track-link"
        >Track</a>
      </div>

      <form v-else class="column q-gutter-sm" @submit.prevent="submit">
        <q-input
          v-model="trackingNumber"
          outlined
          dense
          autofocus
          label="Tracking number"
          :disable="pending"
          data-testid="package-add-number"
        />
        <q-select
          ref="carrierRef"
          v-model="carrier"
          outlined
          dense
          emit-value
          map-options
          label="Carrier"
          :options="CARRIER_OPTIONS"
          :disable="pending"
          data-testid="package-add-carrier"
        />
        <q-banner v-if="errorMessage" dense class="bg-negative text-white" data-testid="package-add-error">
          {{ errorMessage }}
        </q-banner>
      </form>
    </div>

    <template #actions>
      <template v-if="result">
        <q-btn flat no-caps label="Add another" data-testid="package-add-another" @click="reset" />
        <q-btn unelevated no-caps color="primary" label="Done" data-testid="package-add-done" @click="close" />
      </template>
      <template v-else>
        <q-btn flat no-caps label="Cancel" :disable="pending" data-testid="package-add-cancel" @click="close" />
        <q-btn
          unelevated
          no-caps
          color="primary"
          label="Add"
          :loading="pending"
          :disable="pending || !normalizedNumber"
          data-testid="package-add-submit"
          @click="submit"
        />
      </template>
    </template>
  </DialogShell>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { usePackagesApi } from '~/composables/usePackagesApi'
import type { AddPackageBody, PackageOut } from '~/composables/usePackagesApi'
import { ApiError } from '~/composables/useApi'
import { errorText } from '~/utils/errors'
import DialogShell from '~/components/ui/DialogShell.vue'

interface CarrierOption {
  label: string
  value: string | null
}

const CARRIER_OPTIONS: CarrierOption[] = [
  { label: 'Detect automatically', value: null },
  { label: 'UPS', value: 'ups' },
  { label: 'USPS', value: 'usps' },
  { label: 'FedEx', value: 'fedex' },
  { label: 'DHL', value: 'dhl' },
  { label: 'OnTrac', value: 'ontrac' },
  { label: 'LaserShip', value: 'lasership' },
  { label: 'Amazon Logistics', value: 'amazon' },
  { label: 'Canada Post', value: 'canada_post' },
  { label: 'Purolator', value: 'purolator' },
  { label: 'DPD', value: 'dpd' },
  { label: 'Yodel', value: 'yodel' },
  { label: 'Old Dominion', value: 'old_dominion' },
  { label: 'Spee-Dee', value: 'speedee' },
  { label: 'GOFO', value: 'gofo' },
]

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  added: [pkg: PackageOut]
}>()

const api = usePackagesApi()

const trackingNumber = ref('')
const carrier = ref<string | null>(null)
const pending = ref(false)
const errorMessage = ref<string | null>(null)
const result = ref<PackageOut | null>(null)
const carrierRef = ref<{ focus: () => void } | null>(null)

/** Trimmed tracking number with all internal whitespace removed. */
const normalizedNumber = computed(() => trackingNumber.value.replace(/\s+/g, ''))

/**
 * Display label for a carrier code.
 * @param code - Carrier code as returned by the API.
 * @returns The known label, or the raw code when unknown.
 */
function carrierLabel(code: string): string {
  return CARRIER_OPTIONS.find((option) => option.value === code)?.label ?? code
}

/**
 * Posts the tracking number; the server detects the carrier when none is chosen.
 * @returns Nothing; on success stores the result and emits `added`, on failure sets errorMessage.
 */
async function submit() {
  if (pending.value || !normalizedNumber.value) return
  pending.value = true
  errorMessage.value = null
  const body: AddPackageBody = { tracking_number: normalizedNumber.value }
  if (carrier.value) body.carrier = carrier.value
  try {
    const pkg = await api.addPackage(body)
    result.value = pkg
    emit('added', pkg)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'unrecognized_tracking_number') {
      errorMessage.value = err.message
      await nextTick()
      carrierRef.value?.focus()
    } else if (err instanceof ApiError && err.code === 'package_exists') {
      errorMessage.value = 'Already tracked'
    } else {
      errorMessage.value = errorText(err, 'Failed to add package')
    }
  } finally {
    pending.value = false
  }
}

function reset() {
  trackingNumber.value = ''
  carrier.value = null
  pending.value = false
  errorMessage.value = null
  result.value = null
}

function close() {
  emit('update:modelValue', false)
}

function onModelUpdate(value: boolean) {
  emit('update:modelValue', value)
}

watch(
  () => props.modelValue,
  (isOpen) => {
    if (isOpen) reset()
  },
  { immediate: true },
)
</script>
