<template>
  <div v-if="items.length === 0" data-testid="health-empty">
    <EmptyState icon="health_and_safety" title="No health data yet" message="No sources are active, or no run has completed." />
  </div>
  <q-list v-else bordered separator data-testid="health-list">
    <q-item v-for="item in items" :key="item.source" clickable to="/collectors" data-testid="health-row">
      <q-item-section avatar>
        <StatusChip kind="health" :value="item.status" dense />
      </q-item-section>
      <q-item-section>
        <q-item-label data-testid="health-source">{{ humanize(item.source) }}</q-item-label>
        <q-item-label v-if="item.detail" caption data-testid="health-detail">{{ item.detail }}</q-item-label>
      </q-item-section>
      <q-item-section v-if="showChecked" side>
        <q-item-label caption data-testid="health-checked">{{ relativeTime(item.checked_at) }}</q-item-label>
      </q-item-section>
    </q-item>
  </q-list>
</template>

<script setup lang="ts">
import EmptyState from './ui/EmptyState.vue'
import StatusChip from './ui/StatusChip.vue'
import type { SourceHealth } from '~/composables/useApi'

/**
 * Per-source health rows shared by the overview page (and any future panel needing the same list):
 * a status chip, the humanized source name, its detail text and — optionally — when it was last
 * checked. Rows link to /collectors, where the underlying collector can be inspected or fixed.
 */
interface Props {
  items: SourceHealth[]
  /** Show the "checked X ago" column; defaults to true. */
  showChecked?: boolean
}

withDefaults(defineProps<Props>(), { showChecked: true })
</script>
