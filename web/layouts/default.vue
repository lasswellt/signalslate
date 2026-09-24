<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from '#imports'
import { useQuasar } from 'quasar'

interface NavItem {
  label: string
  to: string
  icon: string
  slug: string
  exact?: boolean
}

interface NavGroup {
  label: string
  items: NavItem[]
}

const navGroups: NavGroup[] = [
  {
    label: 'Digest',
    items: [
      { label: 'Overview', to: '/', icon: 'dashboard', slug: 'overview', exact: true },
      { label: 'Runs', to: '/history', icon: 'history', slug: 'runs' },
      { label: 'Schedule', to: '/config', icon: 'schedule', slug: 'schedule' },
    ],
  },
  {
    label: 'Sources',
    items: [
      { label: 'Accounts', to: '/connections', icon: 'link', slug: 'accounts' },
      { label: 'Collectors', to: '/collectors', icon: 'sync', slug: 'collectors' },
    ],
  },
  {
    label: 'Tools',
    items: [
      { label: 'Domains', to: '/domains', icon: 'language', slug: 'domains' },
      { label: 'Jobs', to: '/jobs', icon: 'work', slug: 'jobs' },
    ],
  },
]

const flatNavItems = navGroups.flatMap((group) => group.items.map((item) => ({ ...item, group: group.label })))

const $q = useQuasar()
const route = useRoute()

const drawerOpen = ref(true)
const drawerCollapsed = ref(false)
const mdPlus = computed(() => $q.screen.gt.sm)
const isMini = computed(() => drawerCollapsed.value && mdPlus.value)

function toggleDrawer() {
  drawerOpen.value = !drawerOpen.value
}

function toggleCollapse() {
  drawerCollapsed.value = !drawerCollapsed.value
}

const isDark = computed(() => $q.dark.isActive)

function toggleDark() {
  $q.dark.toggle()
  $q.localStorage.set('ss-dark', $q.dark.isActive)
}

onMounted(() => {
  const saved = $q.localStorage.getItem('ss-dark')
  if (saved !== null) {
    $q.dark.set(saved as boolean)
  }
})

interface Breadcrumb {
  label: string
}

const breadcrumbs = computed<Breadcrumb[]>(() => {
  const path = route.path
  const exactMatch = flatNavItems.find((item) => item.exact && path === item.to)
  const prefixMatch = flatNavItems
    .filter((item) => !item.exact && (path === item.to || path.startsWith(`${item.to}/`)))
    .sort((a, b) => b.to.length - a.to.length)[0]
  const match = exactMatch ?? prefixMatch
  if (!match) return []

  const crumbs: Breadcrumb[] = [{ label: match.group }, { label: match.label }]
  const rest = path
    .slice(match.to.length)
    .split('/')
    .filter(Boolean)
  for (const segment of rest) {
    crumbs.push({ label: /^\d+$/.test(segment) ? 'Details' : humanize(segment) })
  }
  return crumbs
})
</script>

<template>
  <q-layout view="hHh Lpr lFf">
    <q-header class="ss-header">
      <q-toolbar>
        <q-btn flat dense round icon="menu" aria-label="Toggle navigation" @click="toggleDrawer">
          <q-tooltip>Toggle navigation</q-tooltip>
        </q-btn>
        <q-toolbar-title class="text-weight-medium row items-center no-wrap q-gutter-x-sm">
          <img src="/logo.svg" alt="" width="28" height="28" />
          <span>SignalSlate</span>
        </q-toolbar-title>
        <q-breadcrumbs v-if="breadcrumbs.length" class="ss-breadcrumbs gt-xs q-mr-md">
          <q-breadcrumbs-el v-for="(crumb, i) in breadcrumbs" :key="i" :label="crumb.label" />
        </q-breadcrumbs>
        <q-space />
        <q-btn
          flat
          dense
          round
          :icon="isDark ? 'light_mode' : 'dark_mode'"
          aria-label="Toggle dark mode"
          @click="toggleDark"
        >
          <q-tooltip>Toggle dark mode</q-tooltip>
        </q-btn>
      </q-toolbar>
    </q-header>

    <q-drawer v-model="drawerOpen" show-if-above bordered :width="240" :mini="isMini">
      <q-list>
        <template v-for="group in navGroups" :key="group.label">
          <q-item-label v-if="!isMini" header>{{ group.label }}</q-item-label>
          <q-item
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            :exact="item.exact ?? false"
            clickable
            v-ripple
            active-class="ss-nav-active"
            :data-testid="`nav-${item.slug}`"
          >
            <q-item-section avatar>
              <q-icon :name="item.icon" />
              <q-tooltip v-if="isMini">{{ item.label }}</q-tooltip>
            </q-item-section>
            <q-item-section v-if="!isMini">{{ item.label }}</q-item-section>
          </q-item>
        </template>
      </q-list>

      <div v-if="mdPlus" class="row justify-end q-pa-sm">
        <q-btn
          flat
          dense
          round
          :icon="drawerCollapsed ? 'chevron_right' : 'chevron_left'"
          :aria-label="drawerCollapsed ? 'Expand navigation' : 'Collapse navigation'"
          @click="toggleCollapse"
        >
          <q-tooltip>{{ drawerCollapsed ? 'Expand navigation' : 'Collapse navigation' }}</q-tooltip>
        </q-btn>
      </div>
    </q-drawer>

    <q-page-container>
      <slot />
    </q-page-container>
  </q-layout>
</template>

<style scoped>
.ss-header {
  background: var(--ss-surface);
  color: var(--ss-text);
  border-bottom: 1px solid var(--ss-border);
  box-shadow: none;
}

.ss-breadcrumbs {
  color: var(--ss-muted);
}

.ss-breadcrumbs :deep(.q-breadcrumbs__el-icon),
.ss-breadcrumbs :deep(.q-breadcrumbs__el:last-child) {
  color: var(--ss-text);
}

:deep(.ss-nav-active) {
  color: var(--ss-accent);
  background: var(--ss-accent-subtle);
  box-shadow: inset 3px 0 0 0 var(--ss-accent);
}
</style>
