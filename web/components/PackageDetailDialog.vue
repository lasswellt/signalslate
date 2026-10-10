<template>
  <DialogShell
    :model-value="modelValue"
    :title="title"
    :subtitle="pkg?.merchant ?? undefined"
    :busy="pending"
    width="640px"
    @update:model-value="onModelUpdate"
  >
    <div data-testid="package-detail-dialog">
      <div v-if="loading" data-testid="package-detail-loading">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="40%" />
        <q-skeleton type="text" width="80%" />
      </div>

      <div v-else-if="error" data-testid="package-detail-error">
        <q-banner dense class="bg-negative text-white">{{ error }}</q-banner>
        <q-btn flat no-caps label="Retry" data-testid="package-detail-retry" @click="load" />
      </div>

      <template v-else-if="pkg">
        <q-banner v-if="actionError" dense class="bg-negative text-white q-mb-md" data-testid="package-action-error">
          {{ actionError }}
        </q-banner>

        <div class="q-mb-md" data-testid="package-summary">
          <div class="row items-center q-gutter-sm q-mb-xs">
            <q-chip dense :color="statusView(pkg.status).color" text-color="white" :icon="statusView(pkg.status).icon" :aria-label="`Status: ${statusView(pkg.status).label}`" data-testid="package-status">
              {{ statusView(pkg.status).label }}
            </q-chip>
            <q-badge v-if="pkg.archived" color="grey" data-testid="package-archived-badge">Archived</q-badge>
          </div>
          <div v-if="pkg.tracking_number" class="text-body2" data-testid="package-tracking-number">
            <span class="text-grey-8">{{ carrierLabel }}</span>
            <span class="q-ml-sm text-mono" style="font-family: monospace">{{ pkg.tracking_number }}</span>
          </div>
          <div class="text-body2 text-grey-8" data-testid="package-eta">ETA: {{ formatDate(pkg.eta) }}</div>
          <div class="text-body2 text-grey-8" data-testid="package-last-checked">
            Last checked: {{ pkg.last_checked_at ? relativeTime(pkg.last_checked_at) : '—' }}
          </div>
          <div v-if="pkg.last_error" class="text-body2 text-negative" data-testid="package-last-error">{{ pkg.last_error }}</div>
          <q-btn
            v-if="pkg.tracking_url"
            flat
            no-caps
            color="primary"
            type="a"
            :href="pkg.tracking_url"
            target="_blank"
            rel="noopener noreferrer"
            icon="open_in_new"
            label="Track on carrier site"
            data-testid="package-track-link"
          />
        </div>

        <q-list bordered class="rounded-borders">
          <q-expansion-item default-opened label="Events" data-testid="package-events">
            <q-card-section>
              <div v-if="events.length === 0" class="text-grey-8" data-testid="package-events-empty">No carrier events yet</div>
              <q-list v-else separator dense>
                <q-item v-for="(event, index) in events" :key="`${event.occurred_at ?? 'unknown'}-${index}`" data-testid="package-event">
                  <q-item-section>
                    <q-item-label>
                      <q-chip dense outline :icon="statusView(event.status).icon">{{ statusView(event.status).label }}</q-chip>
                      <span class="text-caption text-grey-8">{{ formatDate(event.occurred_at) }}</span>
                    </q-item-label>
                    <q-item-label v-if="event.description">{{ event.description }}</q-item-label>
                    <q-item-label v-if="event.location" caption>{{ event.location }}</q-item-label>
                  </q-item-section>
                </q-item>
              </q-list>
            </q-card-section>
          </q-expansion-item>

          <q-expansion-item default-opened label="Source emails" data-testid="package-emails">
            <q-card-section>
              <div v-if="pkg.emails.length === 0" class="text-grey-8" data-testid="package-emails-empty">No source emails</div>
              <q-list v-else separator dense>
                <q-item v-for="(email, index) in pkg.emails" :key="index" data-testid="package-email">
                  <q-item-section>
                    <q-item-label>{{ email.subject ?? '(no subject)' }}</q-item-label>
                    <q-item-label caption>{{ email.sender ?? '—' }} · {{ formatDate(email.received_at) }}</q-item-label>
                  </q-item-section>
                </q-item>
              </q-list>
            </q-card-section>
          </q-expansion-item>
        </q-list>

        <q-banner v-if="confirmingDelete" dense class="bg-grey-3 q-mt-md" data-testid="package-delete-confirm">
          Delete this package? This cannot be undone.
          <template #action>
            <q-btn flat no-caps label="Cancel" :disable="pending" data-testid="package-delete-cancel" @click="confirmingDelete = false" />
            <q-btn flat no-caps color="negative" label="Delete" :disable="pending" data-testid="package-delete-confirm-btn" @click="remove" />
          </template>
        </q-banner>
      </template>
    </div>

    <template #actions>
      <q-btn
        v-if="pkg"
        flat
        no-caps
        :label="pkg.archived ? 'Unarchive' : 'Archive'"
        :disable="pending"
        data-testid="package-archive"
        @click="toggleArchived"
      />
      <q-btn
        v-if="pkg"
        flat
        no-caps
        color="negative"
        label="Delete"
        :disable="pending"
        data-testid="package-delete"
        @click="confirmingDelete = true"
      />
      <q-btn flat no-caps label="Close" :disable="pending" data-testid="package-detail-close" @click="close" />
    </template>
  </DialogShell>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import DialogShell from '~/components/ui/DialogShell.vue'
import { usePackagesApi } from '~/composables/usePackagesApi'
import type { PackageDetailOut } from '~/composables/usePackagesApi'

const props = defineProps<{
  modelValue: boolean
  packageId: number | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  changed: []
}>()

const api = usePackagesApi()
const $q = useQuasar()

interface StatusView {
  color: string
  icon: string
  label: string
}

// Package lifecycle statuses are not in the shared statusMeta() kinds, so they are mapped locally:
// icon + label always, and never the accent teal (DESIGN.md "Status chips").
const STATUS_VIEWS: Record<string, StatusView> = {
  pending: { color: 'grey', icon: 'schedule', label: 'Pending' },
  in_transit: { color: 'info', icon: 'local_shipping', label: 'In transit' },
  out_for_delivery: { color: 'info', icon: 'delivery_dining', label: 'Out for delivery' },
  delivered: { color: 'positive', icon: 'check_circle', label: 'Delivered' },
  exception: { color: 'negative', icon: 'error', label: 'Exception' },
  returned: { color: 'warning', icon: 'assignment_return', label: 'Returned' },
  stale: { color: 'warning', icon: 'hourglass_empty', label: 'Stale' },
  unknown: { color: 'grey', icon: 'help', label: 'Unknown' },
}

function statusView(status: string): StatusView {
  return STATUS_VIEWS[status] ?? { color: 'grey', icon: 'help', label: humanize(status) }
}

const loading = ref(false)
const error = ref<string | null>(null)
const pkg = ref<PackageDetailOut | null>(null)
const pending = ref(false)
const actionError = ref<string | null>(null)
const confirmingDelete = ref(false)

const carrierLabel = computed(() => {
  const carrier = pkg.value?.carrier ?? ''
  return carrier.length <= 5 ? carrier.toUpperCase() : humanize(carrier)
})

const title = computed(() => {
  const value = pkg.value
  if (!value) return 'Package'
  if (value.tracking_number) return `${carrierLabel.value} ${value.tracking_number}`
  return value.order_id ? `Order ${value.order_id}` : 'Package'
})

/** Events newest first; events without a timestamp sort last. */
const events = computed(() =>
  [...(pkg.value?.events ?? [])].sort((a, b) => Date.parse(b.occurred_at ?? '') - Date.parse(a.occurred_at ?? '') || 0),
)

/** Loads the package detail (events and source-email metadata) for the current id. */
async function load() {
  if (props.packageId === null) return
  loading.value = true
  error.value = null
  actionError.value = null
  confirmingDelete.value = false
  pkg.value = null
  try {
    pkg.value = await api.getPackage(props.packageId)
  } catch (err) {
    error.value = errorText(err)
  } finally {
    loading.value = false
  }
}

/** Runs a mutation: disables the buttons meanwhile, then emits changed and closes; failures show inline and by toast. */
async function runAction(action: (id: number) => Promise<unknown>) {
  if (props.packageId === null || pending.value) return
  pending.value = true
  actionError.value = null
  try {
    await action(props.packageId)
    emit('changed')
    emit('update:modelValue', false)
  } catch (err) {
    actionError.value = errorText(err)
    $q.notify({ type: 'negative', message: actionError.value })
  } finally {
    pending.value = false
    confirmingDelete.value = false
  }
}

function toggleArchived() {
  const archived = !(pkg.value?.archived ?? false)
  return runAction((id) => api.setArchived(id, archived))
}

function remove() {
  return runAction((id) => api.deletePackage(id))
}

function close() {
  emit('update:modelValue', false)
}

function onModelUpdate(value: boolean) {
  emit('update:modelValue', value)
}

watch(
  () => [props.modelValue, props.packageId] as const,
  ([isOpen]) => {
    if (isOpen) void load()
  },
  { immediate: true },
)
</script>
