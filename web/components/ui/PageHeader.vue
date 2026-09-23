<template>
  <div class="page-header" data-testid="page-header">
    <div class="page-header__row">
      <q-btn
        v-if="back"
        flat
        round
        dense
        icon="arrow_back"
        :aria-label="`Back to ${title}`"
        data-testid="page-header-back"
        :to="back"
      >
        <q-tooltip>{{ `Back to ${title}` }}</q-tooltip>
      </q-btn>
      <div class="page-header__titles">
        <h1 class="text-h5 page-header__title" data-testid="page-header-title">{{ title }}</h1>
        <div v-if="subtitle" class="text-body2 text-grey-7" data-testid="page-header-subtitle">{{ subtitle }}</div>
      </div>
      <div v-if="$slots.actions" class="page-header__actions" data-testid="page-header-actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * Standard page header: title, optional subtitle and optional back navigation, with a right-aligned
 * actions slot that wraps below the title on narrow viewports. See DESIGN.md "Page anatomy".
 */
interface Props {
  title: string
  subtitle?: string
  /** Route to navigate to for a labelled back button; omitted entirely when not set. */
  back?: string
}

defineProps<Props>()
</script>

<style scoped lang="scss">
.page-header__row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}

.page-header__titles {
  flex: 1 1 auto;
  min-width: 0;
}

.page-header__title {
  margin: 0;
}

.page-header__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-left: auto;
}

@media (max-width: 599px) {
  .page-header__actions {
    flex-basis: 100%;
    margin-left: 0;
  }
}
</style>
