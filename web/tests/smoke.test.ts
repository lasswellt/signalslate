import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QInput } from 'quasar'
import DefaultLayout from '~/layouts/default.vue'

// Proves the harness (nuxt environment + Quasar) works, and pins the secret-input
// attributes every credential field relies on.
describe('harness smoke', () => {
  it('renders a Quasar password input with no prefilled value', async () => {
    const wrapper = await mountSuspended(QInput, {
      props: { type: 'password', autocomplete: 'new-password', modelValue: '' },
    })
    const input = wrapper.get('input')
    expect(input.attributes('type')).toBe('password')
    expect(input.attributes('autocomplete')).toBe('new-password')
    expect((input.element as HTMLInputElement).value).toBe('')
  })
})

describe('default layout', () => {
  const NAV_ITEMS: Array<{ testid: string; href: string }> = [
    { testid: 'nav-overview', href: '/' },
    { testid: 'nav-runs', href: '/history' },
    { testid: 'nav-schedule', href: '/config' },
    { testid: 'nav-accounts', href: '/connections' },
    { testid: 'nav-collectors', href: '/collectors' },
    { testid: 'nav-domains', href: '/domains' },
    { testid: 'nav-jobs', href: '/jobs' },
  ]

  it('renders all nav items with hrefs, group headers, and the dark toggle', async () => {
    const wrapper = await mountSuspended(DefaultLayout, {
      attachTo: document.body,
      slots: { default: () => 'page content' },
    })

    for (const { testid, href } of NAV_ITEMS) {
      const el = wrapper.get(`[data-testid="${testid}"]`)
      expect(el.attributes('href')).toBe(href)
    }

    const headers = wrapper.findAll('.q-item__label--header').map((el) => el.text())
    expect(headers).toEqual(['Digest', 'Sources', 'Tools'])

    expect(wrapper.find('[aria-label="Toggle dark mode"]').exists()).toBe(true)

    wrapper.unmount()
  })
})
