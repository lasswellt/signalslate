<template>
  <q-page padding class="q-gutter-md" style="max-width: 900px">
    <div class="row items-center q-gutter-sm">
      <q-btn flat round icon="arrow_back" to="/history" />
      <div class="text-h5">Run #{{ id }}</div>
    </div>

    <q-card v-if="run">
      <q-card-section>
        <div class="row items-center q-gutter-sm">
          <q-badge :color="statusColor(run.status)">{{ run.status }}</q-badge>
          <span>{{ run.trigger }}</span>
          <span class="text-grey">{{ formatDate(run.started_at) }}</span>
        </div>
        <div v-if="run.summary" class="q-mt-sm">{{ run.summary }}</div>
        <div v-if="run.error" class="q-mt-sm text-negative">{{ run.error }}</div>
        <q-btn
          v-if="run.has_pdf"
          class="q-mt-md"
          color="primary"
          icon="download"
          label="Download PDF"
          :href="api.pdfUrl(run.id)"
          target="_blank"
        />
      </q-card-section>
    </q-card>

    <q-card v-if="run">
      <q-card-section>
        <div class="text-subtitle1 q-mb-sm">Source health for this run</div>
        <div v-if="!run.source_health.length" class="text-grey">No sources were active for this run.</div>
        <q-list v-else bordered separator>
          <q-item v-for="h in run.source_health" :key="h.source">
            <q-item-section avatar>
              <q-badge :color="h.status === 'ok' ? 'positive' : 'negative'" rounded />
            </q-item-section>
            <q-item-section>
              <q-item-label>{{ h.source }}</q-item-label>
              <q-item-label caption>{{ h.detail }}</q-item-label>
            </q-item-section>
          </q-item>
        </q-list>
      </q-card-section>
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
const route = useRoute()
const id = Number(route.params.id)
const api = useApi()
const run = ref<Awaited<ReturnType<typeof api.getRun>> | null>(null)

function statusColor(s: string) {
  return { success: 'positive', partial: 'warning', failed: 'negative', running: 'grey' }[s] ?? 'grey'
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}

onMounted(async () => {
  run.value = await api.getRun(id)
})
</script>
