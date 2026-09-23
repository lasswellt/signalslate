<template>
  <q-chip
    dense
    :size="dense ? 'sm' : undefined"
    :color="meta.color"
    text-color="white"
    :icon="meta.icon"
    :aria-label="`Status: ${meta.label}`"
    data-testid="status-chip"
  >
    {{ meta.label }}
  </q-chip>
</template>

<script setup lang="ts">
import type { StatusKind } from '~/utils/status'

/**
 * Standard status indicator: icon + label from the shared `statusMeta` mapping, never colour alone.
 * See DESIGN.md "Status chips".
 */
interface Props {
  kind: StatusKind
  value: string | number | null
  dense?: boolean
  /** Overrides the mapped label text while keeping the mapped color/icon. */
  labelOverride?: string
}

const props = defineProps<Props>()

const meta = computed(() => {
  const base = statusMeta(props.kind, props.value)
  return props.labelOverride ? { ...base, label: props.labelOverride } : base
})
</script>
