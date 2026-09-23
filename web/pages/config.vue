<template>
  <q-page class="page-container q-pa-md">
    <PageHeader title="Schedule" subtitle="When the digest runs and what it includes" />

    <AsyncState :loading="loading" :error="loadError" @retry="load">
      <template v-if="form">
        <div class="q-gutter-md">
          <q-card>
            <q-card-section>
              <div class="text-subtitle1 q-mb-sm">When</div>
              <q-select
                v-model="presetKey"
                :options="PRESET_OPTIONS"
                emit-value
                map-options
                dense
                outlined
                label="Schedule"
                data-testid="schedule-preset"
              />
              <q-input
                v-if="presetKey === 'custom'"
                v-model="form.schedule_cron"
                dense
                outlined
                class="q-mt-sm"
                label="Cron schedule"
                hint="Standard 5-field cron, e.g. 0 7 * * 1-5"
                :error="!cronValid"
                error-message="Enter 5 space-separated fields (minute hour day month weekday)"
                data-testid="schedule-cron"
              />
              <div class="text-body2 text-grey-8 q-mt-sm" data-testid="schedule-description">
                {{ cronDescription }}
              </div>
              <div v-if="nextRun" class="text-caption text-grey-7 q-mt-xs" data-testid="schedule-next-run">
                Next run: {{ formatDate(nextRun) }}
              </div>
            </q-card-section>
          </q-card>

          <q-card>
            <q-card-section>
              <div class="text-subtitle1 q-mb-sm">Task tracker</div>
              <q-select
                v-model="form.tracker"
                :options="trackerOptions"
                emit-value
                map-options
                dense
                outlined
                label="Tracker"
                data-testid="tracker-select"
              />
            </q-card-section>
          </q-card>

          <q-card>
            <q-card-section>
              <div class="text-subtitle1 q-mb-sm">Sources included in the digest</div>
              <div class="column q-gutter-sm">
                <q-toggle
                  v-for="(_, key) in form.active_sources"
                  :key="key"
                  v-model="form.active_sources[key]"
                  :label="humanize(key)"
                  :data-testid="`source-toggle-${key}`"
                />
              </div>
              <div class="text-caption text-grey-7 q-mt-sm">
                Manage source connections on the <NuxtLink to="/collectors">Collectors</NuxtLink> page.
              </div>
            </q-card-section>
          </q-card>
        </div>

        <div class="schedule-footer">
          <q-btn flat no-caps label="Reset" :disable="!dirty || saving" data-testid="reset-btn" @click="onReset" />
          <q-btn
            color="primary"
            no-caps
            label="Save"
            :loading="saving"
            :disable="!canSave"
            data-testid="save-btn"
            @click="onSave"
          />
        </div>
      </template>
    </AsyncState>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import PageHeader from '~/components/ui/PageHeader.vue'
import AsyncState from '~/components/ui/AsyncState.vue'
import type { DigestConfig } from '~/composables/useApi'

type PresetKey = 'weekdays' | 'daily' | 'mondays' | 'custom'

interface Preset {
  key: PresetKey
  label: string
  cron: string | null
}

// Common schedules the sales team actually uses; anything else is edited as raw cron under "Custom".
const PRESETS: Preset[] = [
  { key: 'weekdays', label: 'Weekdays 7:00', cron: '0 7 * * 1-5' },
  { key: 'daily', label: 'Every day 7:00', cron: '0 7 * * *' },
  { key: 'mondays', label: 'Mondays 7:00', cron: '0 7 * * 1' },
  { key: 'custom', label: 'Custom', cron: null },
]
const PRESET_OPTIONS = PRESETS.map((preset) => ({ label: preset.label, value: preset.key }))

// Known tracker values today; an unrecognized saved value (or one added server-side later) still
// shows a readable label instead of disappearing from the list.
const TRACKER_LABELS: Record<string, string> = {
  mstodo: 'Microsoft To Do',
  todoist: 'Todoist',
  none: 'No tracker',
}

const DOW_NAMES = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays']

const api = useApi()
const $q = useQuasar()
const router = useRouter()

const form = ref<DigestConfig | null>(null)
const original = ref<DigestConfig | null>(null)
const nextRun = ref<string | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const saving = ref(false)

/**
 * Loads the digest config and (best-effort) the next scheduled run. A failed status fetch is
 * silent: the schedule page still works without it, it just omits the "Next run" line.
 */
async function load() {
  loading.value = true
  loadError.value = null
  try {
    const [config, status] = await Promise.all([
      api.getConfig(),
      api.getStatus().catch(() => null),
    ])
    form.value = config
    original.value = JSON.parse(JSON.stringify(config))
    nextRun.value = status?.next_scheduled_run ?? null
    forcedCustom.value = false
  } catch (error) {
    loadError.value = errorText(error)
  } finally {
    loading.value = false
  }
}

const dirty = computed(() => {
  if (!form.value || !original.value) return false
  return JSON.stringify(form.value) !== JSON.stringify(original.value)
})

// Explicit "Custom" selection while the cron still happens to match a preset (e.g. the user picked
// Custom without editing anything yet) would otherwise be masked by the derived match below, so it
// overrides the derived value until a real preset is chosen or the form is reloaded/reset.
const forcedCustom = ref(false)

/**
 * Which schedule preset the current cron matches, or "custom" when it matches none. Setting it
 * writes the preset's cron straight into the form; picking "Custom" leaves the cron as-is so the
 * field below becomes editable without losing what was there.
 */
const presetKey = computed<PresetKey>({
  get() {
    if (forcedCustom.value) return 'custom'
    const match = PRESETS.find((preset) => preset.cron !== null && preset.cron === form.value?.schedule_cron)
    return match ? match.key : 'custom'
  },
  set(key) {
    if (!form.value) return
    if (key === 'custom') {
      forcedCustom.value = true
      return
    }
    forcedCustom.value = false
    const preset = PRESETS.find((entry) => entry.key === key)
    if (preset?.cron) form.value.schedule_cron = preset.cron
  },
})

function isValidCronShape(cron: string): boolean {
  const parts = cron.trim().split(/\s+/)
  return parts.length === 5 && parts.every((part) => part.length > 0)
}

const cronValid = computed(() => {
  if (presetKey.value !== 'custom' || !form.value) return true
  return isValidCronShape(form.value.schedule_cron)
})

/**
 * Plain-English description of a 5-field cron string for the common "minute hour * * weekday-list"
 * shape (the only shape the presets produce); anything else falls back to naming it a custom
 * schedule rather than guessing.
 * @param cron - The raw cron string.
 * @returns A short, human-readable description.
 */
function describeCron(cron: string): string {
  const fallback = `Custom schedule: ${cron}`
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return fallback
  const [minute, hour, dom, month, dow] = parts
  if (dom !== '*' || month !== '*') return fallback

  const minuteNum = Number(minute)
  const hourNum = Number(hour)
  if (!Number.isInteger(minuteNum) || !Number.isInteger(hourNum)) return fallback
  if (minuteNum < 0 || minuteNum > 59 || hourNum < 0 || hourNum > 23) return fallback
  const time = `${String(hourNum).padStart(2, '0')}:${String(minuteNum).padStart(2, '0')}`

  if (dow === '*') return `Every day at ${time}`
  if (dow === '1-5') return `Weekdays at ${time}`
  if (dow === '0,6' || dow === '6,0') return `Weekends at ${time}`

  const days = dow.split(',').map((entry) => DOW_NAMES[Number(entry)])
  if (days.some((name) => name === undefined)) return fallback
  return `${days.join(', ')} at ${time}`
}

const cronDescription = computed(() => (form.value ? describeCron(form.value.schedule_cron) : ''))

const trackerOptions = computed(() => {
  const options = Object.entries(TRACKER_LABELS).map(([value, label]) => ({ value, label }))
  const current = form.value?.tracker
  if (current && !options.some((option) => option.value === current)) {
    options.push({ value: current, label: humanize(current) })
  }
  return options
})

const canSave = computed(() => dirty.value && cronValid.value && !saving.value)

function onReset() {
  if (!original.value) return
  form.value = JSON.parse(JSON.stringify(original.value))
  forcedCustom.value = false
}

async function onSave() {
  if (!form.value || !canSave.value) return
  saving.value = true
  try {
    const saved = await api.updateConfig(form.value)
    form.value = saved
    original.value = JSON.parse(JSON.stringify(saved))
    $q.notify({ type: 'positive', message: 'Schedule saved.' })
  } catch (error) {
    $q.notify({ type: 'negative', message: errorText(error) })
  } finally {
    saving.value = false
  }
}

/** Shows the discard-changes confirm via the Quasar Dialog plugin; resolves true only on Discard. */
function confirmDiscard(): Promise<boolean> {
  return new Promise((resolve) => {
    $q.dialog({
      title: 'Discard unsaved changes?',
      message: 'Your schedule changes have not been saved.',
      persistent: true,
      cancel: { label: 'Keep editing', flat: true, noCaps: true },
      ok: { label: 'Discard', color: 'negative', noCaps: true },
    })
      .onOk(() => resolve(true))
      .onCancel(() => resolve(false))
      .onDismiss(() => resolve(false))
  })
}

// A page-scoped navigation guard rather than onBeforeRouteLeave: this page is not always rendered
// through a <RouterView> in tests, and onBeforeRouteLeave silently no-ops without one. A global
// beforeEach added on mount and removed on unmount behaves the same for a real user, and is
// reliably testable either way.
let allowNextLeave = false
let removeGuard: (() => void) | null = null

onMounted(() => {
  void load()
  removeGuard = router.beforeEach(async (to, from) => {
    if (to.fullPath === from.fullPath || !dirty.value || allowNextLeave) return true
    const discard = await confirmDiscard()
    if (!discard) return false
    allowNextLeave = true
    return true
  })
})

onBeforeUnmount(() => {
  removeGuard?.()
})
</script>

<style scoped lang="scss">
.schedule-footer {
  position: sticky;
  bottom: 0;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  padding: 12px 0;
  background: var(--ss-surface-bg, #f6f7f9);
  border-top: 1px solid var(--ss-border, #e3e6ea);
}
</style>
