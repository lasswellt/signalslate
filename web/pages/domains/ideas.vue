<template>
  <div data-testid="panel-ideas">
    <DomainIdeasPanel />
  </div>
</template>

<script setup lang="ts">
// Thin move of the former Ideas tab (pages/domains.vue): unchanged content, T-015 refines it.
import DomainIdeasPanel from '~/components/DomainIdeasPanel.vue'
</script>
