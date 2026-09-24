import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const configSource = readFileSync(resolve(__dirname, '../nuxt.config.ts'), 'utf-8')
const scssSource = readFileSync(resolve(__dirname, '../assets/css/app.scss'), 'utf-8')

/**
 * Computes the WCAG 2.x relative luminance of an sRGB colour.
 * @param hex - A hex colour string, e.g. "#FFFFFF" or "#fff".
 * @returns Relative luminance in the range [0, 1].
 * @throws Error if the hex string cannot be parsed.
 */
function relativeLuminance(hex: string): number {
  const normalized = hex.replace('#', '')
  const full =
    normalized.length === 3
      ? normalized
          .split('')
          .map((c) => c + c)
          .join('')
      : normalized
  if (full.length !== 6) throw new Error(`Invalid hex colour: ${hex}`)
  const r = parseInt(full.slice(0, 2), 16) / 255
  const g = parseInt(full.slice(2, 4), 16) / 255
  const b = parseInt(full.slice(4, 6), 16) / 255
  const channel = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  const [rl, gl, bl] = [channel(r), channel(g), channel(b)]
  return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl
}

/**
 * Computes the WCAG 2.x contrast ratio between two sRGB colours.
 * @param hexA - First hex colour.
 * @param hexB - Second hex colour.
 * @returns Contrast ratio in the range [1, 21].
 * @throws Error if either hex string cannot be parsed.
 */
function contrastRatio(hexA: string, hexB: string): number {
  const la = relativeLuminance(hexA)
  const lb = relativeLuminance(hexB)
  const lighter = Math.max(la, lb)
  const darker = Math.min(la, lb)
  return (lighter + 0.05) / (darker + 0.05)
}

/**
 * Extracts a Quasar brand hex value from the nuxt.config.ts source text.
 * @param name - Brand key name, e.g. "primary" or "dark-page".
 * @returns The hex colour string as written in source.
 * @throws Error if the key is not found in configSource.
 */
function getBrandHex(name: string): string {
  const re = new RegExp(`(?:'${name}'|${name}):\\s*'(#[0-9a-fA-F]{3,6})'`)
  const match = configSource.match(re)
  if (!match) throw new Error(`Brand token not found in nuxt.config.ts: ${name}`)
  return match[1]
}

/**
 * Extracts a CSS custom property hex value from a named block in the scss source.
 * @param blockSelector - The selector whose block to search within, e.g. "body.body--dark".
 * @param varName - The CSS custom property name, e.g. "--q-positive".
 * @returns The hex colour string as written in source.
 * @throws Error if the block or variable is not found.
 */
function getCssVarHex(blockSelector: string, varName: string): string {
  const blockRe = new RegExp(`${blockSelector.replace(/[.]/g, '\\.')}\\s*\\{([^}]*)\\}`, 's')
  const blockMatch = scssSource.match(blockRe)
  if (!blockMatch) throw new Error(`Block not found in app.scss: ${blockSelector}`)
  const varRe = new RegExp(`${varName}:\\s*(#[0-9a-fA-F]{3,6})`)
  const varMatch = blockMatch[1].match(varRe)
  if (!varMatch) throw new Error(`Var ${varName} not found in block ${blockSelector}`)
  return varMatch[1]
}

const WHITE = '#FFFFFF'
const AA_NORMAL = 4.5

describe('contrastRatio', () => {
  it('computes known reference ratios correctly', () => {
    expect(contrastRatio('#000000', '#FFFFFF')).toBeCloseTo(21, 0)
    expect(contrastRatio('#777777', '#FFFFFF')).toBeCloseTo(4.48, 1)
  })
})

describe('brand hexes vs white (light mode)', () => {
  const cases: Array<[string, string]> = [
    ['secondary', getBrandHex('secondary')],
    ['positive', getBrandHex('positive')],
    ['negative', getBrandHex('negative')],
    ['warning', getBrandHex('warning')],
    ['info', getBrandHex('info')],
  ]

  it.each(cases)('%s (%s) has contrast >= 4.5 on white', (_name, hex) => {
    expect(contrastRatio(hex, WHITE)).toBeGreaterThanOrEqual(AA_NORMAL)
  })

  it('white text on light primary has contrast >= 4.5', () => {
    expect(contrastRatio(WHITE, getBrandHex('primary'))).toBeGreaterThanOrEqual(AA_NORMAL)
  })
})

describe('dark-mode status tokens as fills (white text)', () => {
  // --q-positive/negative/warning/info back Quasar bg-* fills, which pair
  // with white text throughout the app (bg-negative text-white, chips, etc).
  const cases: Array<[string, string]> = [
    ['--q-positive', getCssVarHex('body.body--dark', '--q-positive')],
    ['--q-negative', getCssVarHex('body.body--dark', '--q-negative')],
    ['--q-warning', getCssVarHex('body.body--dark', '--q-warning')],
    ['--q-info', getCssVarHex('body.body--dark', '--q-info')],
  ]

  it.each(cases)('%s (%s) has contrast >= 4.5 with white text', (_name, hex) => {
    expect(contrastRatio(hex, WHITE)).toBeGreaterThanOrEqual(AA_NORMAL)
  })

  it('--ss-on-primary on --q-primary has contrast >= 4.5 in dark mode', () => {
    const onPrimary = getCssVarHex('body.body--dark', '--ss-on-primary')
    const primary = getCssVarHex('body.body--dark', '--q-primary')
    expect(contrastRatio(onPrimary, primary)).toBeGreaterThanOrEqual(AA_NORMAL)
  })

  it('app.scss forces on-primary text on dark bg-primary fills', () => {
    expect(
      /body\.body--dark \.bg-primary\s*\{[^}]*color:\s*var\(--ss-on-primary\)\s*!important/.test(
        scssSource
      )
    ).toBe(true)
  })

  it('app.scss draws the dark primary checkbox tick in on-primary ink, not white', () => {
    expect(
      /body\.body--dark \.q-checkbox__inner--truthy[^{]*\.q-checkbox__svg[^{]*\{[^}]*color:\s*var\(--ss-on-primary\)/.test(
        scssSource
      )
    ).toBe(true)
  })
})

describe('dark-mode status TEXT tokens vs dark surface', () => {
  const darkSurface = getCssVarHex('body.body--dark', '--ss-surface')

  const cases: Array<[string, string]> = [
    ['--ss-positive-text', getCssVarHex('body.body--dark', '--ss-positive-text')],
    ['--ss-negative-text', getCssVarHex('body.body--dark', '--ss-negative-text')],
    ['--ss-warning-text', getCssVarHex('body.body--dark', '--ss-warning-text')],
    ['--ss-info-text', getCssVarHex('body.body--dark', '--ss-info-text')],
  ]

  it.each(cases)('%s (%s) has contrast >= 4.5 on dark surface %s', (_name, hex) => {
    expect(contrastRatio(hex, darkSurface)).toBeGreaterThanOrEqual(AA_NORMAL)
  })
})

describe('light-mode muted text vs background', () => {
  it('--ss-muted on --ss-bg has contrast >= 4.5', () => {
    const muted = getCssVarHex('body.body--light', '--ss-muted')
    const bg = getCssVarHex('body.body--light', '--ss-bg')
    expect(contrastRatio(muted, bg)).toBeGreaterThanOrEqual(AA_NORMAL)
  })
})
