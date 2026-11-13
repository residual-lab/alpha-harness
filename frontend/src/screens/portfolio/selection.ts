/**
 * Selection Expression: Fast Expression over an Alpha's metrics rather than its data, read by
 * the same grammar.
 *
 * Statements end at `;` or a new line. `name = …` names a value for the statements after it;
 * every other statement is a condition, and an Alpha is selected when it meets them all. As in
 * Fast Expression, a comparison is 1 or 0, NaN is neither, and a metric an Alpha lacks is NaN,
 * so any condition on it leaves the Alpha out. `&&`, `||` and `!` are `and`, `or` and `not`;
 * `25%` is 0.25 and `5bps` is 0.0005, as BRAIN's fractions are shown in the table.
 */

import { parseSelection, type TreeNode } from '@/lib/fastexpr'

export type Metrics = Record<string, number | null>

export interface Selection {
  /** How many statements are conditions. */
  conditions: number
  test: (metrics: Metrics) => boolean
}

type Value = (scope: Metrics) => number

const truth = (x: number) => !Number.isNaN(x) && x !== 0
const bool = (x: boolean) => (x ? 1 : 0)
/** NaN on either side is no answer, never true: `x != y` with x missing must not select. */
const compare =
  (test: (a: number, b: number) => boolean) =>
  (a: number, b: number): number =>
    Number.isNaN(a) || Number.isNaN(b) ? Number.NaN : bool(test(a, b))

const COMPARISONS: Record<string, (a: number, b: number) => number> = {
  '>=': compare((a, b) => a >= b),
  '<=': compare((a, b) => a <= b),
  '>': compare((a, b) => a > b),
  '<': compare((a, b) => a < b),
  '==': compare((a, b) => a === b),
  '!=': compare((a, b) => a !== b),
}

// Three-valued, so a missing metric stays missing: `not(or(x > 1, y > 1))` with neither
// measured must not select. A false side settles `and` and a true one `or` regardless.
const and = (a: number, b: number) =>
  a === 0 || b === 0 ? 0 : Number.isNaN(a) || Number.isNaN(b) ? Number.NaN : 1
const or = (a: number, b: number) =>
  truth(a) || truth(b) ? 1 : Number.isNaN(a) || Number.isNaN(b) ? Number.NaN : 0
const not = (a: number) => (Number.isNaN(a) ? Number.NaN : bool(a === 0))
const if_else = (c: number, a: number, b: number) =>
  Number.isNaN(c) ? Number.NaN : c !== 0 ? a : b

/** Fast Expression's own names, so a formula reads the same in both. `[min, max]` arguments. */
export const FUNCTIONS: Record<string, [number, number, (...args: number[]) => number]> = {
  abs: [1, 1, Math.abs],
  sign: [1, 1, Math.sign],
  sqrt: [1, 1, Math.sqrt],
  log: [1, 1, Math.log],
  power: [2, 2, Math.pow],
  max: [2, Number.POSITIVE_INFINITY, Math.max],
  min: [2, Number.POSITIVE_INFINITY, Math.min],
  and: [2, 2, and],
  or: [2, 2, or],
  not: [1, 1, not],
  if_else: [3, 3, if_else],
  is_nan: [1, 1, (a) => bool(Number.isNaN(a))],
}

export class SelectionError extends Error {
  readonly line: number
  constructor(message: string, line: number) {
    super(`Line ${line}: ${message}`)
    this.line = line
  }
}

const number = (text: string) =>
  text.endsWith('%')
    ? Number(text.slice(0, -1)) / 100
    : text.endsWith('bps')
      ? Number(text.slice(0, -3)) / 10_000
      : Number(text)

/** The name meant, when `name` differs from one only in case or underscores. */
function meant(name: string, names: Iterable<string>): string {
  const squash = (n: string) => n.toLowerCase().replaceAll('_', '')
  for (const n of names) if (squash(n) === squash(name)) return `; did you mean "${n}"?`
  return ''
}

/** Every operator between two values. A ratio over nought has no answer: NaN leaves the Alpha
 *  out, where Infinity would pass. `^` is right-associative and, as on BRAIN, below a sign:
 *  `-2 ^ 2` is 4. */
const BINARY: Record<string, (a: number, b: number) => number> = {
  ...COMPARISONS,
  '&&': and,
  '||': or,
  '+': (a, b) => a + b,
  '-': (a, b) => a - b,
  '*': (a, b) => a * b,
  '/': (a, b) => (b === 0 ? Number.NaN : a / b),
  '^': (a, b) => a ** b,
}
const UNARY: Record<string, (a: number) => number> = { '-': (a) => -a, '+': (a) => a, '!': not }

/** What the grammar says when there is nothing but comments and blank lines. */
const EMPTY = 'The expression is empty.'

/** Throws a {@link SelectionError} naming the line at fault. `metrics` are the names allowed. */
export function compile(text: string, metrics: readonly string[]): Selection {
  const known = new Set(metrics)
  const named: { name: string; value: Value }[] = []
  const conditions: Value[] = []
  const line = (at = 0) => text.slice(0, at).split('\n').length
  const read = parseSelection(text)
  if ('error' in read) {
    if (read.error === EMPTY) return { conditions: 0, test: () => true }
    // Positions count characters across the whole text, which a line number says better.
    throw new SelectionError(read.error.replace(/ at \d+/g, ''), line(read.at))
  }
  const fail = (message: string, node: TreeNode): never => {
    throw new SelectionError(message, line(node.at))
  }
  const arg = (node: TreeNode, index: number) => node.args[index] as TreeNode

  const value = (node: TreeNode): Value => {
    switch (node.kind) {
      case 'num': {
        const constant = number(node.value)
        return () => constant
      }
      case 'name': {
        const name = node.value
        if (!known.has(name)) fail(`no metric named "${name}"${meant(name, known)}`, node)
        return (s) => s[name] ?? Number.NaN
      }
      case 'call':
        return call(node)
      case 'unary': {
        const op = UNARY[node.value] as (a: number) => number
        const inner = value(arg(node, 0))
        return (s) => op(inner(s))
      }
      case 'binary': {
        const op = BINARY[node.value] as (a: number, b: number) => number
        const [l, r] = [value(arg(node, 0)), value(arg(node, 1))]
        return (s) => op(l(s), r(s))
      }
      case 'ternary': {
        const [test, yes, no] = node.args.map(value) as [Value, Value, Value]
        return (s) => if_else(test(s), yes(s), no(s))
      }
      default:
        return fail(`expected a number, metric or "(", found ${node.value}`, node)
    }
  }

  const call = (node: TreeNode): Value => {
    const name = node.value
    if (!Object.hasOwn(FUNCTIONS, name))
      return fail(`no function named "${name}"${meant(name, Object.keys(FUNCTIONS))}`, node)
    const [least, most, fn] = FUNCTIONS[name] as (typeof FUNCTIONS)[string]
    if (node.kwargs.length) return fail(`${name} takes no named inputs`, node)
    const args = node.args.map(value)
    if (args.length < least || args.length > most)
      fail(
        `${name} takes ${least === most ? least : `at least ${least}`} argument${least === 1 ? '' : 's'}`,
        node,
      )
    return (s) => fn(...args.map((a) => a(s)))
  }

  const statements = read.tree.kind === 'seq' ? read.tree.args : [read.tree]
  for (const statement of statements) {
    if (statement.kind !== 'assign') {
      conditions.push(value(statement))
      continue
    }
    const target = statement.value
    if (Object.hasOwn(FUNCTIONS, target)) fail(`"${target}" is a function`, statement)
    named.push({ name: target, value: value(arg(statement, 0)) })
    known.add(target)
  }
  return {
    conditions: conditions.length,
    test: (metrics) => {
      // Copied only when a statement names a value into it; read-only otherwise.
      const scope: Metrics = named.length ? { ...metrics } : metrics
      for (const { name, value } of named) scope[name] = value(scope)
      return conditions.every((c) => truth(c(scope)))
    },
  }
}
