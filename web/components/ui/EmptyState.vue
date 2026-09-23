<template>
  <div class="empty-state text-center text-grey-7" data-testid="empty-state">
    <q-icon :name="icon" size="48px" class="empty-state__icon" />
    <div class="text-subtitle1 text-grey-9" data-testid="empty-state-title">{{ title }}</div>
    <div v-if="message" class="text-body2" data-testid="empty-state-message">{{ message }}</div>
    <div v-if="$slots.action" class="empty-state__action" data-testid="empty-state-action">
      <slot name="action" />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * Centered, muted empty state: an icon, a short message, and an optional CTA slot. See DESIGN.md
 * "Empty / loading / error states".
 */
interface Props {
  icon: string
  title: string
  message?: string
}

defineProps<Props>()
</script>

<style scoped lang="scss">
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 48px 16px;
}

.empty-state__icon {
  opacity: 0.6;
}

.empty-state__action {
  margin-top: 8px;
}
</style>
