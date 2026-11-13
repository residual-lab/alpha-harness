import { expect, test } from 'vitest'
import { fingerprint } from './fingerprint'

const ids = Array.from({ length: 5000 }, (_, i) => `A${(i * 7919).toString(36)}`)

test('the same list gives the same fingerprint', () => {
  expect(fingerprint([...ids])).toBe(fingerprint(ids))
  expect(fingerprint(ids)).toMatch(/^5000:[0-9a-f]{16}$/)
})

test('any change to a list gives a new fingerprint', () => {
  const seen = new Set([fingerprint(ids)])
  ids.forEach((id, i) => {
    seen.add(fingerprint(ids.with(i, `${id}x`)))
    if (i > 0) seen.add(fingerprint(ids.with(i, ids[i - 1] ?? '').with(i - 1, id)))
  })
  seen.add(fingerprint(ids.slice(1)))
  seen.add(fingerprint(['ab', 'c']))
  seen.add(fingerprint(['a', 'bc']))
  expect(seen.size).toBe(2 * ids.length + 3)
})
