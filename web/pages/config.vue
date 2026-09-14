<template>
  <q-page padding class="q-gutter-md" style="max-width: 600px">
    <div class="text-h5">Config</div>

    <q-form v-if="form" class="q-gutter-md" @submit.prevent="onSave">
      <q-input
        v-model="form.schedule_cron"
        label="Schedule (cron)"
        hint="Standard 5-field cron, e.g. 0 6 * * * for 6:00 AM daily"
      />

      <q-select
        v-model="form.tracker"
        :options="['mstodo', 'todoist', 'none']"
        label="Todo tracker"
      />

      <div class="text-subtitle1">Active sources</div>
      <q-list bordered separator>
        <q-item v-for="(_, key) in form.active_sources" :key="key" tag="label" v-ripple>
          <q-item-section avatar>
            <q-checkbox v-model="form.active_sources[key]" />
          </q-item-section>
          <q-item-section>{{ key }}</q-item-section>
        </q-item>
      </q-list>

      <q-btn type="submit" color="primary" label="Save" :loading="saving" />
      <q-banner v-if="saved" class="bg-positive text-white">Saved.</q-banner>
    </q-form>
  </q-page>
</template>

<script setup lang="ts">
import type { DigestConfig } from '~/composables/useApi'

const api = useApi()
const form = ref<DigestConfig | null>(null)
const saving = ref(false)
const saved = ref(false)

onMounted(async () => {
  form.value = await api.getConfig()
})

async function onSave() {
  if (!form.value) return
  saving.value = true
  saved.value = false
  form.value = await api.updateConfig(form.value)
  saving.value = false
  saved.value = true
  setTimeout(() => (saved.value = false), 2000)
}
</script>
