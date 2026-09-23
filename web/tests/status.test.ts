import { describe, expect, it } from 'vitest'
import { ApiError } from '~/composables/useApi'
import { errorText } from '~/utils/errors'
import { statusMeta } from '~/utils/status'

describe('statusMeta', () => {
  it('maps run/health statuses, with partial as warning (not an error)', () => {
    expect(statusMeta('run', 'partial')).toMatchObject({ color: 'warning', label: 'Partial' })
    expect(statusMeta('health', 'partial')).toMatchObject({ color: 'warning', label: 'Partial' })
    expect(statusMeta('run', 'ok')).toMatchObject({ color: 'positive', label: 'OK' })
    expect(statusMeta('run', 'success')).toMatchObject({ color: 'positive', label: 'Success' })
    expect(statusMeta('run', 'running')).toMatchObject({ color: 'info', label: 'Running' })
    expect(statusMeta('run', 'error')).toMatchObject({ color: 'negative', label: 'Error' })
    expect(statusMeta('run', 'failed')).toMatchObject({ color: 'negative', label: 'Failed' })
    expect(statusMeta('run', 'skipped')).toMatchObject({ color: 'grey', label: 'Skipped' })
  })

  it('falls back to grey + help icon + a humanized label for unrecognized values', () => {
    expect(statusMeta('run', 'unknown')).toMatchObject({ color: 'grey', icon: 'help', label: 'Unknown' })
    expect(statusMeta('run', 'something_weird')).toMatchObject({ color: 'grey', icon: 'help', label: 'Something weird' })
    expect(statusMeta('run', null)).toMatchObject({ color: 'grey', icon: 'help' })
    expect(statusMeta('run', undefined)).toMatchObject({ color: 'grey', icon: 'help' })
  })

  it('maps purchase statuses (both the page vocabulary and the pipeline vocabulary)', () => {
    expect(statusMeta('purchase', 'submitted')).toMatchObject({ color: 'positive' })
    expect(statusMeta('purchase', 'confirmed')).toMatchObject({ color: 'positive' })
    expect(statusMeta('purchase', 'succeeded')).toMatchObject({ color: 'positive' })
    expect(statusMeta('purchase', 'failed')).toMatchObject({ color: 'negative' })
    expect(statusMeta('purchase', 'refused')).toMatchObject({ color: 'negative' })
    expect(statusMeta('purchase', 'pending')).toMatchObject({ color: 'warning' })
    expect(statusMeta('purchase', 'unknown')).toMatchObject({ color: 'grey' })
  })

  it('maps application statuses', () => {
    expect(statusMeta('application', 'submitted')).toMatchObject({ color: 'positive' })
    expect(statusMeta('application', 'interviewing')).toMatchObject({ color: 'positive' })
    expect(statusMeta('application', 'rejected')).toMatchObject({ color: 'negative' })
    expect(statusMeta('application', 'withdrawn')).toMatchObject({ color: 'negative' })
    expect(statusMeta('application', 'applied')).toMatchObject({ color: 'grey' })
  })

  it('maps desktop-assist states', () => {
    expect(statusMeta('assist', 'done')).toMatchObject({ color: 'positive' })
    expect(statusMeta('assist', 'failed')).toMatchObject({ color: 'negative' })
    expect(statusMeta('assist', 'queued')).toMatchObject({ color: 'info' })
    expect(statusMeta('assist', 'idle')).toMatchObject({ color: 'grey' })
  })

  it('maps domain idea availability', () => {
    expect(statusMeta('domain', 'likely_available')).toMatchObject({ color: 'positive', label: 'Likely available' })
    expect(statusMeta('domain', 'taken')).toMatchObject({ color: 'negative', label: 'Taken' })
    expect(statusMeta('domain', 'anything_else')).toMatchObject({ color: 'grey', label: 'Unknown' })
  })

  it('accepts numbers for confidence (0-1) and labels as a percent', () => {
    expect(statusMeta('confidence', 0.9)).toMatchObject({ color: 'positive', label: '90%' })
    expect(statusMeta('confidence', 0.5)).toMatchObject({ color: 'warning', label: '50%' })
    expect(statusMeta('confidence', 0.1)).toMatchObject({ color: 'negative', label: '10%' })
  })

  it('accepts numbers for fit (0-100), and treats null as unscored', () => {
    expect(statusMeta('fit', 82)).toMatchObject({ color: 'positive', label: '82' })
    expect(statusMeta('fit', 60)).toMatchObject({ color: 'warning', label: '60' })
    expect(statusMeta('fit', 10)).toMatchObject({ color: 'negative', label: '10' })
    expect(statusMeta('fit', null)).toMatchObject({ color: 'grey', label: 'Unscored' })
  })
})

describe('errorText', () => {
  it('reads the message off an ApiError (already formatted by toApiError)', () => {
    const err = new ApiError(422, 'name: field required')
    expect(errorText(err)).toBe('name: field required')
  })

  it('reads the message off a plain Error', () => {
    expect(errorText(new Error('boom'))).toBe('boom')
  })

  it('passes through a string as-is', () => {
    expect(errorText('boom')).toBe('boom')
  })

  it('falls back to the default message for unrecognized values', () => {
    expect(errorText({ weird: true })).toBe('Something went wrong')
    expect(errorText(undefined)).toBe('Something went wrong')
  })

  it('honors a custom fallback', () => {
    expect(errorText(undefined, 'Could not load')).toBe('Could not load')
    expect(errorText('', 'Could not load')).toBe('Could not load')
  })
})
