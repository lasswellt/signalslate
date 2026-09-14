<template>
  <q-layout view="hHh lpR fFf">
    <q-header elevated class="bg-primary text-white">
      <q-toolbar>
        <q-toolbar-title>SignalSlate</q-toolbar-title>
        <q-tabs shrink stretch>
          <q-route-tab to="/" exact label="Dashboard" />
          <q-route-tab to="/history" label="History" />
          <q-route-tab to="/config" label="Config" />
        </q-tabs>
      </q-toolbar>
    </q-header>

    <q-page-container>
      <NuxtPage />
    </q-page-container>
  </q-layout>
</template>
