import { afterEach, describe, expect, it } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import PageHeader from '~/components/ui/PageHeader.vue'
import StatusChip from '~/components/ui/StatusChip.vue'
import EmptyState from '~/components/ui/EmptyState.vue'

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)

let mounted: Array<VueWrapper<unknown>> = []

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
  mounted = []
})

describe('PageHeader', () => {
  it('renders the title as an h1 and an optional subtitle', async () => {
    const wrapper = await mountSuspended(PageHeader, {
      props: { title: 'Domains', subtitle: 'Manage your registered domains' },
      attachTo: document.body,
    })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('page-header')).not.toBeNull()
    const title = $('page-header-title')
    expect(title?.tagName).toBe('H1')
    expect(title?.textContent).toBe('Domains')
    expect($('page-header-subtitle')?.textContent).toBe('Manage your registered domains')
  })

  it('renders the actions slot right-aligned', async () => {
    const wrapper = await mountSuspended(PageHeader, {
      props: { title: 'Domains' },
      attachTo: document.body,
      slots: { actions: '<button data-testid="my-action">Add</button>' },
    })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('page-header-actions')).not.toBeNull()
    expect($('my-action')).not.toBeNull()
  })

  it('renders a labelled back button with an aria-label when a back route is given', async () => {
    const wrapper = await mountSuspended(PageHeader, {
      props: { title: 'Domain detail', back: '/domains' },
      attachTo: document.body,
    })
    mounted.push(wrapper as VueWrapper<unknown>)

    const back = $('page-header-back')
    expect(back).not.toBeNull()
    expect(back?.getAttribute('aria-label')).toBe('Back to Domain detail')
  })

  it('omits the back button and actions slot when not given', async () => {
    const wrapper = await mountSuspended(PageHeader, { props: { title: 'Domains' }, attachTo: document.body })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('page-header-back')).toBeNull()
    expect($('page-header-actions')).toBeNull()
  })
})

describe('StatusChip', () => {
  it('renders icon + label for an ok run status, never colour alone', async () => {
    const wrapper = await mountSuspended(StatusChip, { props: { kind: 'run', value: 'ok' }, attachTo: document.body })
    mounted.push(wrapper as VueWrapper<unknown>)

    const chip = $('status-chip')
    expect(chip?.querySelector('.q-chip__content')?.textContent?.trim()).toBe('OK')
    expect(chip?.querySelector('.q-icon')).not.toBeNull()
    expect(chip?.getAttribute('aria-label')).toBe('Status: OK')
  })

  it('renders a partial run status as a warning, not an error', async () => {
    const wrapper = await mountSuspended(StatusChip, { props: { kind: 'run', value: 'partial' }, attachTo: document.body })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('status-chip')?.querySelector('.q-chip__content')?.textContent?.trim()).toBe('Partial')
    expect($('status-chip')?.getAttribute('aria-label')).toBe('Status: Partial')
  })

  it('renders an error status', async () => {
    const wrapper = await mountSuspended(StatusChip, { props: { kind: 'run', value: 'error' }, attachTo: document.body })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('status-chip')?.querySelector('.q-chip__content')?.textContent?.trim()).toBe('Error')
    expect($('status-chip')?.getAttribute('aria-label')).toBe('Status: Error')
  })

  it('falls back to Unknown + help icon for an unrecognized value', async () => {
    const wrapper = await mountSuspended(StatusChip, { props: { kind: 'run', value: 'something_weird' }, attachTo: document.body })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('status-chip')?.querySelector('.q-chip__content')?.textContent?.trim()).toBe('Something weird')
    expect($('status-chip')?.getAttribute('aria-label')).toBe('Status: Something weird')
  })

  it('accepts a labelOverride while keeping the mapped color/icon', async () => {
    const wrapper = await mountSuspended(StatusChip, {
      props: { kind: 'run', value: 'ok', labelOverride: 'All good' },
      attachTo: document.body,
    })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('status-chip')?.querySelector('.q-chip__content')?.textContent?.trim()).toBe('All good')
    expect($('status-chip')?.getAttribute('aria-label')).toBe('Status: All good')
  })
})

describe('EmptyState', () => {
  it('renders icon, title, message and an action slot', async () => {
    const wrapper = await mountSuspended(EmptyState, {
      props: { icon: 'inbox', title: 'No domains yet', message: 'Add your first domain to get started.' },
      attachTo: document.body,
      slots: { action: '<button data-testid="empty-cta">Add domain</button>' },
    })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('empty-state')).not.toBeNull()
    expect($('empty-state-title')?.textContent).toBe('No domains yet')
    expect($('empty-state-message')?.textContent).toBe('Add your first domain to get started.')
    expect($('empty-state-action')).not.toBeNull()
    expect($('empty-cta')).not.toBeNull()
  })

  it('omits the message and action slot when not given', async () => {
    const wrapper = await mountSuspended(EmptyState, { props: { icon: 'inbox', title: 'No domains yet' }, attachTo: document.body })
    mounted.push(wrapper as VueWrapper<unknown>)

    expect($('empty-state-message')).toBeNull()
    expect($('empty-state-action')).toBeNull()
  })
})
