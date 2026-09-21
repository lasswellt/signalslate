import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { QInput } from 'quasar'

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
