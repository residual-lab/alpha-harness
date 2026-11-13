import fc from 'fast-check'
import { expect, test, vi } from 'vitest'
import type { TreeNode } from './fastexpr'
import wasm from './fastexpr.wasm?url&inline'

// Under Node the module's URL is the dev server's, so it is handed the bytes inline instead.
const fetchInline = fetch
vi.stubGlobal('fetch', () => fetchInline(wasm))
const { parse, tokens, write } = await import('./fastexpr')
const { compile } = await import('@/screens/portfolio/selection')
vi.unstubAllGlobals()

const unplaced = (n: TreeNode): TreeNode => ({
  kind: n.kind,
  value: n.value,
  args: n.args.map(unplaced),
  kwargs: n.kwargs.map((k) => ({ name: k.name, value: unplaced(k.value) })),
})
const tree = (text: string) => {
  const read = parse(text)
  if ('error' in read) throw new Error(read.error)
  return unplaced(read.tree)
}

test('a sign binds tighter than a power, as on BRAIN', () => {
  expect(tree('-x ^ 2')).toEqual(tree('(-x) ^ 2'))
  expect(write(tree('-(x ^ 2)'))).toBe('-(x ^ 2)')
  expect(compile('-2 ^ 2 == 4', []).test({})).toBe(true)
})

test("a message is the backend's, word for word", () => {
  expect(parse('rank(close))')).toEqual({ error: "Expected ';' at 11, found ')'.", at: 11 })
})

const leaf = fc.oneof(
  fc.constantFrom('close', 'x', '$f', '...', 'nan').map((value) => ({ kind: 'name', value })),
  fc.constantFrom('0', '1.5', '.5', '1e-3').map((value) => ({ kind: 'num', value })),
  fc.constant({ kind: 'str', value: '"0, 1 # 2"' }),
)
const { node } = fc.letrec<{ node: TreeNode }>((go) => ({
  node: fc.oneof(
    { depthSize: 'small', withCrossShrink: true },
    leaf.map((l) => ({ ...l, args: [], kwargs: [] }) as TreeNode),
    fc.tuple(fc.constantFrom('-', '!'), go('node')).map(([value, a]) => ({
      kind: 'unary' as const,
      value,
      args: [a],
      kwargs: [],
    })),
    fc
      .tuple(
        fc.constantFrom('+', '-', '*', '/', '^', '<', '==', '&&', '||'),
        go('node'),
        go('node'),
      )
      .map(([value, a, b]) => ({ kind: 'binary' as const, value, args: [a, b], kwargs: [] })),
    fc.tuple(go('node'), go('node'), go('node')).map((args) => ({
      kind: 'ternary' as const,
      value: '',
      args,
      kwargs: [],
    })),
    fc
      .tuple(fc.array(go('node'), { maxLength: 3 }), fc.option(go('node'), { nil: undefined }))
      .map(([args, option]) => ({
        kind: 'call' as const,
        value: 'f',
        args,
        kwargs: option ? [{ name: 'k', value: option }] : [],
      })),
  ),
}))

test('a tree written out reads back as the same tree', () => {
  fc.assert(
    fc.property(node, (n) => {
      expect(tree(write(n))).toEqual(n)
    }),
  )
})

test('every character is in at most one token, in order, whatever is typed', () => {
  fc.assert(
    fc.property(fc.string({ unit: 'binary' }), (text) => {
      let end = 0
      for (const [, from, to] of tokens(text)) {
        expect(from).toBeGreaterThanOrEqual(end)
        expect(to).toBeGreaterThan(from)
        end = to
      }
      expect(end).toBeLessThanOrEqual(text.length)
    }),
  )
})

test('a Selection Expression selects by its metrics, a missing one leaving the Alpha out', () => {
  const rule = compile('r = returns / drawdown\nr > 1 && sharpe >= 1.58 # why\nmargin >= 5bps', [
    'returns',
    'drawdown',
    'sharpe',
    'margin',
  ])
  expect(rule.conditions).toBe(2)
  expect(rule.test({ returns: 0.2, drawdown: 0.1, sharpe: 2, margin: 0.001 })).toBe(true)
  expect(rule.test({ returns: 0.2, drawdown: 0.1, sharpe: 2, margin: null })).toBe(false)
  expect(() => compile('sharpe > 1\nSharpe > 1', ['sharpe'])).toThrow(
    'Line 2: no metric named "Sharpe"; did you mean "sharpe"?',
  )
  expect(() => compile('constructor(1)', [])).toThrow('Line 1: no function named "constructor"')
  expect(compile('+-1 < 0', []).test({})).toBe(true)
})
