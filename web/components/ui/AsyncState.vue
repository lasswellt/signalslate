<script setup lang="ts">
import EmptyState from './EmptyState.vue'
/**
 * Wraps a data-driven view's three states (loading / error / empty) plus the loaded content,
 * per DESIGN.md's "Empty / loading / error states" section. Pages/panels pass their fetch state in
 * and render their real content in the default slot; AsyncState decides which state to show.
 */
interface Props {
  loading: boolean
  error?: string | null
  empty?: boolean
  emptyIcon?: string
  emptyTitle?: string
  emptyMessage?: string
  skeleton?: 'list' | 'table' | 'cards'
}

const props = withDefaults(defineProps<Props>(), {
  error: null,
  empty: false,
  emptyIcon: 'inbox',
  emptyTitle: undefined,
  emptyMessage: 'Nothing here yet.',
  skeleton: 'list',
})

defineEmits<{
  retry: []
}>()

defineSlots<{
  default(): unknown
  empty(): unknown
}>()
</script>

<template>
  <div v-if="loading" data-testid="loading">
    <template v-if="skeleton === 'table'">
      <q-skeleton type="text" width="30%" class="q-mb-md" />
      <q-skeleton v-for="n in 4" :key="n" type="text" class="q-mb-sm" />
    </template>
    <template v-else-if="skeleton === 'cards'">
      <div class="row q-col-gutter-md">
        <div v-for="n in 3" :key="n" class="col-12 col-sm-4">
          <q-skeleton type="rect" height="120px" />
        </div>
      </div>
    </template>
    <template v-else>
      <q-skeleton type="text" width="75%" />
      <q-skeleton type="text" width="50%" />
      <q-skeleton type="text" width="60%" />
    </template>
  </div>

  <div v-else-if="error" data-testid="load-error">
    <q-banner dense class="bg-negative text-white">{{ error }}</q-banner>
    <q-btn flat no-caps color="primary" label="Retry" data-testid="retry" @click="$emit('retry')" />
  </div>

  <template v-else-if="empty">
    <slot name="empty">
      <EmptyState
        :icon="props.emptyIcon"
        :title="props.emptyTitle ?? props.emptyMessage"
        :message="props.emptyTitle ? props.emptyMessage : undefined"
      />
    </slot>
  </template>

  <slot v-else />
</template>
