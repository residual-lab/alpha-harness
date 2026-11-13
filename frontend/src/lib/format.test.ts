import fc from 'fast-check'
import { expect, test } from 'vitest'
import { DASH, fmt, moment, secondsSince } from './format'

const absent = fc.constantFrom(null, undefined, Number.NaN, Infinity, -Infinity)
const finite = fc.double({ noNaN: true, noDefaultInfinity: true })
const digits = fc.integer({ min: 0, max: 4 })
const instants = fc.date({
  min: new Date('1970-01-01T00:00:00Z'),
  max: new Date('2100-01-01T00:00:00Z'),
  noInvalidDate: true,
})
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

test('a figure is an em dash exactly when there is no number', () => {
  fc.assert(
    fc.property(absent, finite, (missing, value) => {
      for (const show of [fmt.int, fmt.ratio, fmt.pct, fmt.bps, fmt.compact]) {
        expect(show(missing)).toBe(DASH)
        expect(show(value)).not.toBe(DASH)
      }
    }),
  )
})

test('a figure is its value rounded to the digits shown, never a negative zero', () => {
  const shown = [
    [fmt.ratio, 1, ''],
    [fmt.pct, 100, '%'],
    [fmt.bps, 10_000, ' bps'],
  ] as const
  fc.assert(
    fc.property(fc.double({ min: -1e6, max: 1e6, noNaN: true }), digits, (value, d) => {
      for (const [show, scale, unit] of shown) {
        const text = show(value, d)
        expect(text).not.toMatch(/^-[0.]+\D*$/)
        expect(text.endsWith(unit)).toBe(true)
        const scaled = value * scale
        const error = Math.abs(Number(text.slice(0, text.length - unit.length)) - scaled)
        expect(error).toBeLessThanOrEqual(0.5 * 10 ** -d + Math.abs(scaled) * Number.EPSILON * 4)
      }
    }),
  )
})

test('a duration shows the whole seconds given, cut to its smallest unit shown', () => {
  fc.assert(
    fc.property(fc.double({ min: 0, max: 1e9, noNaN: true }), (seconds) => {
      const parts = /^(?:(\d+)h (\d\d)m|(\d+)m (\d\d)s|(\d+)s)$/.exec(fmt.duration(seconds))
      expect(parts).not.toBeNull()
      const [, h = '0', hm = '0', m = '0', ms = '0', s = '0'] = parts ?? []
      for (const below of [hm, m, ms, s]) expect(Number(below)).toBeLessThan(60)
      const whole = Math.floor(seconds)
      expect(Number(h) > 0).toBe(whole >= 3600)
      const unit = whole >= 3600 ? 60 : 1
      const read = Number(h) * 3600 + (Number(hm) + Number(m)) * 60 + Number(ms) + Number(s)
      expect(read).toBe(Math.floor(whole / unit) * unit)
    }),
  )
  expect(fmt.duration(-1)).toBe(DASH)
})

test('a countdown shows hours and minutes, and never runs below zero', () => {
  fc.assert(
    fc.property(fc.double({ min: -1e6, max: 1e9, noNaN: true }), (seconds) => {
      const parts = /^(\d+)h (\d\d)m$/.exec(fmt.countdown(seconds))
      expect(parts).not.toBeNull()
      const [, h = '', m = ''] = parts ?? []
      expect(Number(m)).toBeLessThan(60)
      const whole = Math.max(0, Math.floor(seconds))
      expect(Number(h) * 3600 + Number(m) * 60).toBe(Math.floor(whole / 60) * 60)
    }),
  )
})

test('a timestamp reads back as the moment it names, with an offset or without', () => {
  fc.assert(
    fc.property(instants, fc.integer({ min: 0, max: 999 }), (at, micros) => {
      const iso = at.toISOString()
      // The backend writes UTC without an offset, and Python adds microseconds.
      const naive = iso.slice(0, -1)
      const python = `${naive}${String(micros).padStart(3, '0')}`
      for (const text of [iso, naive, `${naive}+00:00`, python]) {
        expect(moment(text)?.getTime()).toBe(at.getTime())
      }
      expect(secondsSince(iso, at.getTime() + 1500)).toBe(1.5)
      expect(secondsSince(iso, at.getTime() - 1500)).toBe(0)
    }),
  )
})

test('no string, however malformed, is a date that blanks the screen', () => {
  const mangled = fc.oneof(
    fc.string(),
    fc.tuple(instants, fc.nat({ max: 30 })).map(([at, n]) => at.toISOString().slice(0, n)),
    fc.constantFrom('2026-13-45', '+275760-09-13T00:00:00.001Z', '2026-02-30T25:61:00'),
  )
  fc.assert(
    fc.property(mangled, (text) => {
      const at = moment(text)
      expect(at === null || !Number.isNaN(at.getTime())).toBe(true)
      for (const show of [fmt.date, fmt.platformDay, fmt.month, fmt.dateTime, fmt.ago]) {
        expect(() => show(text)).not.toThrow()
      }
    }),
  )
})

test('a calendar day shows as that day wherever the browser is', () => {
  fc.assert(
    fc.property(instants, (at) => {
      const day = at.toISOString().slice(0, 10)
      const [y = 0, m = 0, d = 0] = day.split('-').map(Number)
      expect(fmt.date(day)).toBe(`${MONTHS[m - 1]} ${d}, ${y}`)
    }),
  )
})
