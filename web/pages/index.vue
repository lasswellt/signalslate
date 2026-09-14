<template>
  <q-page padding class="q-gutter-md" style="max-width: 900px">
    <div class="row items-center justify-between">
      <div class="text-h5">Dashboard</div>
      <q-btn
        color="primary"
        icon="play_arrow"
        :label="triggering ? 'Running…' : 'Run now'"
        :loading="triggering"
        @click="onTrigger"
      />
    </div>

    <q-banner v-if="status?.next_scheduled_run" class="bg-grey-2">
      Next scheduled run: {{ formatDate(status.next_scheduled_run) }}
    </q-banner>

    <q-card>
      <q-card-section>
        <div class="text-subtitle1 q-mb-sm">Last run</div>
        <div v-if="!status?.last_run" class="text-grey">No runs yet.</div>
        <div v-else>
          <div class="row items-center q-gutter-sm">
            <q-badge :color="statusColor(status.last_run.status)">{{ status.last_run.status }}</q-badge>
            <span>{{ status.last_run.trigger }}</span>
            <span class="text-grey">{{ formatDate(status.last_run.started_at) }}</span>
          </div>
          <div v-if="status.last_run.summary" class="q-mt-sm">{{ status.last_run.summary }}</div>
          <div v-if="status.last_run.error" class="q-mt-sm text-negative">{{ status.last_run.error }}</div>
        </div>
      </q-card-section>
    </q-card>

    <q-card>
      <q-card-section>
        <div class="text-subtitle1 q-mb-sm">Source health</div>
        <div v-if="!status?.source_health?.length" class="text-grey">
          No health data yet — no sources active, or no run has completed.
        </div>
        <q-list v-else bordered separator>
          <q-item v-for="h in status.source_health" :key="h.source">
            <q-item-section avatar>
              <q-badge :color="h.status === 'ok' ? 'positive' : 'negative'" rounded />
            </q-item-section>
            <q-item-section>
              <q-item-label>{{ h.source }}</q-item-label>
              <q-item-label caption>{{ h.detail }}</q-item-label>
            </q-item-section>
            <q-item-section side>
              <q-item-label caption>{{ formatDate(h.checked_at) }}</q-item-label>
            </q-item-section>
          </q-item>
        </q-list>
      </q-card-section>
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
const api = useApi()
const status = ref<Awaited<ReturnType<typeof api.getStatus>> | null>(null)
const triggering = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

async function refresh() {
  status.value = await api.getStatus()
}

async function onTrigger() {
  triggering.value = true
  await api.triggerRun()
  // Health checks finish in well under a second today; poll briefly for the new run to land.
  let attempts = 0
  pollTimer = setInterval(async () => {
    attempts++
    await refresh()
    if (status.value?.last_run?.status !== 'running' || attempts > 20) {
      if (pollTimer) clearInterval(pollTimer)
      triggering.value = false
    }
  }, 500)
}

function statusColor(s: string) {
  return { success: 'positive', partial: 'warning', failed: 'negative', running: 'grey' }[s] ?? 'grey'
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}

onMounted(refresh)
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>
