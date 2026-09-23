<template>
  <div data-testid="panel-ideas">
    <DomainIdeasPanel @purchased="onPurchased" />
  </div>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import DomainIdeasPanel from '~/components/DomainIdeasPanel.vue'
import type { PurchaseOut } from '~/composables/useDomainsApi'

const $q = useQuasar()

// Portfolio/Watchlist/Purchases each fetch independently per route (see pages/domains.vue); bumping
// this shared tick nudges whichever of them is mounted to refetch, and primes the next one visited.
const refreshTick = inject<Ref<number>>('domains-refresh', ref(0))

/** DomainIdeasPanel bubbles a completed purchase attempt here once its dialog's own success/failure
 * message has been shown; this just gives the user one console-wide notification and refreshes the
 * routes that depend on purchase/domain state. */
function onPurchased(purchase: PurchaseOut) {
  refreshTick.value++
  if (purchase.status === 'succeeded') {
    $q.notify({ type: 'positive', message: `Domain purchased.${purchase.detail ? ` ${purchase.detail}` : ''}` })
  }
}
</script>
