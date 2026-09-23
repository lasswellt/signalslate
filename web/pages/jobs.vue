<template>
  <q-page class="page-container q-pa-md">
    <PageHeader title="Jobs" subtitle="Openings from companies you follow" />

    <q-tabs align="left" no-caps class="text-grey q-mb-sm" active-color="primary" indicator-color="primary" dense>
      <q-route-tab to="/jobs" exact label="Postings" data-testid="tab-postings" />
      <q-route-tab to="/jobs/applications" label="Applications" data-testid="tab-applications" />
      <q-route-tab to="/jobs/companies" label="Companies" data-testid="tab-companies" />
      <q-route-tab to="/jobs/profile" label="Profile" data-testid="tab-profile" />
    </q-tabs>
    <q-separator class="q-mb-md" />

    <NuxtPage />
  </q-page>
</template>

<script setup lang="ts">
import PageHeader from '~/components/ui/PageHeader.vue'
</script>
