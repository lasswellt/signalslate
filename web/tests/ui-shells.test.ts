import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import AsyncState from '~/components/ui/AsyncState.vue'
import DialogShell from '~/components/ui/DialogShell.vue'

const body = document.body
const $ = <T extends Element = HTMLElement>(testid: string) => body.querySelector<T>(`[data-testid="${testid}"]`)

async function click(testid: string) {
  const el = $(testid)
  if (!el) throw new Error(`no element ${testid}`)
  el.click()
  await flushPromises()
}

let mounted: Array<VueWrapper<unknown>> = []

beforeEach(() => {
  mounted = []
})

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount()
})

describe('AsyncState', () => {
  it('shows a skeleton while loading', async () => {
    const wrapper = await mountSuspended(AsyncState, { props: { loading: true, error: null } })
    mounted.push(wrapper as VueWrapper<unknown>)
    expect(wrapper.find('[data-testid="loading"]').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'QSkeleton' }).exists()).toBe(true)
  })

  it('shows the error message and a retry button that emits retry', async () => {
    const wrapper = await mountSuspended(AsyncState, { props: { loading: false, error: 'Could not load domains' } })
    mounted.push(wrapper as VueWrapper<unknown>)
    expect(wrapper.find('[data-testid="load-error"]').text()).toContain('Could not load domains')
    await wrapper.find('[data-testid="retry"]').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('shows an empty state with the given message', async () => {
    const wrapper = await mountSuspended(AsyncState, {
      props: { loading: false, error: null, empty: true, emptyTitle: 'No domains', emptyMessage: 'Add your first domain.' },
    })
    mounted.push(wrapper as VueWrapper<unknown>)
    const empty = wrapper.find('[data-testid="empty-state"]')
    expect(empty.text()).toContain('No domains')
    expect(empty.text()).toContain('Add your first domain.')
  })

  it('renders the default slot when loaded and not empty', async () => {
    const wrapper = await mountSuspended(AsyncState, {
      props: { loading: false, error: null, empty: false },
      slots: { default: '<div data-testid="real-content">Domains list</div>' },
    })
    mounted.push(wrapper as VueWrapper<unknown>)
    expect(wrapper.find('[data-testid="real-content"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="load-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(false)
  })
})

describe('DialogShell', () => {
  async function mountShell(props: Partial<InstanceType<typeof DialogShell>['$props']> = {}) {
    const wrapper = await mountSuspended(DialogShell, {
      props: { modelValue: true, title: 'Edit connection', ...props },
      slots: {
        default: '<div data-testid="shell-body">Body content</div>',
        actions: '<button data-testid="save-btn">Save</button>',
      },
      attachTo: document.body,
    })
    mounted.push(wrapper as VueWrapper<unknown>)
    await flushPromises()
    return wrapper
  }

  it('renders title, subtitle, and both slots', async () => {
    await mountShell({ subtitle: 'acme.com' })
    expect($('dialog-title')?.textContent).toContain('Edit connection')
    expect($('dialog-subtitle')?.textContent).toContain('acme.com')
    expect($('shell-body')).toBeTruthy()
    expect($('save-btn')).toBeTruthy()
  })

  it('has a close button with an accessible label', async () => {
    await mountShell()
    expect($('dialog-close')?.getAttribute('aria-label')).toBe('Close')
  })

  it('closes immediately (no confirm) when not dirty', async () => {
    const wrapper = await mountShell({ dirty: false })
    await click('dialog-close')
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect($('discard-confirm-dialog')).toBeFalsy()
  })

  it('asks for confirmation when dirty and does not close until Discard', async () => {
    const wrapper = await mountShell({ dirty: true })
    await click('dialog-close')
    expect(wrapper.emitted('update:modelValue')).toBeFalsy()
    expect($('discard-confirm-dialog')?.textContent).toContain('Discard unsaved changes?')

    await click('keep-editing')
    expect(wrapper.emitted('update:modelValue')).toBeFalsy()

    await click('dialog-close')
    await click('discard-confirm')
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('disables the close button while busy', async () => {
    await mountShell({ busy: true })
    expect(($('dialog-close') as HTMLButtonElement)?.disabled).toBe(true)
  })
})
