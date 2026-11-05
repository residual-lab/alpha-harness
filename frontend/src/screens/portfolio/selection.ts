/**
 * Selection Expression: Fast Expression's syntax over an Alpha's metrics rather than its data.
 *
 * Statements end at `;` or a new line. `name = …` names a value for the statements after it;
 * every other statement is a condition, and an Alpha is selected when it meets them all. As in
 * Fast Expression, a comparison is 1 or 0, NaN is neither, and a metric an Alpha lacks is NaN,
 * so any condition on it leaves the Alpha out. `&&`, `||` and `!` are `and`, `or` and `not`;
 * `25%` is 0.25 and `5bps` is 0.0005, as BRAIN's fractions are shown in the table.
 */

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
  if_else: [3, 3, (c, a, b) => (Number.isNaN(c) ? Number.NaN : c !== 0 ? a : b)],
  is_nan: [1, 1, (a) => bool(Number.isNaN(a))],
}

export class SelectionError extends Error {
  readonly line: number
  constructor(message: string, line: number) {
    super(`Line ${line}: ${message}`)
    this.line = line
  }
}

interface Token {
  kind: 'num' | 'name' | 'op' | 'end'
  text: string
  line: number
}

const TOKEN =
  /\s*(?:((?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?:%|bps)?)|([A-Za-z_]\w*)|(>=|<=|==|!=|&&|\|\||[-+*/^()<>,=!]))/y

/** `//`, `#` and block comments, as Fast Expression has them; line breaks kept for the count. */
const uncommented = (text: string) =>
  text
    .replace(/\/\*[\s\S]*?(?:\*\/|$)/g, (block) => block.replace(/[^\n]/g, ''))
    .replace(/(?:\/\/|#).*/g, '')

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

/** One statement's tokens per line or `;`, blanks dropped. */
function statements(text: string): Token[][] {
  const out: Token[][] = []
  uncommented(text)
    .split('\n')
    .forEach((source, index) => {
      const line = index + 1
      for (const part of source.split(';')) {
        const tokens: Token[] = []
        TOKEN.lastIndex = 0
        while (TOKEN.lastIndex < part.length) {
          const at = TOKEN.lastIndex
          const m = TOKEN.exec(part)
          if (!m) {
            if (part.slice(at).trim() === '') break
            throw new SelectionError(`unexpected "${part.slice(at).trim()[0]}"`, line)
          }
          const [, num, name, op] = m
          if (num !== undefined) tokens.push({ kind: 'num', text: num, line })
          else if (name !== undefined) tokens.push({ kind: 'name', text: name, line })
          else if (op !== undefined) tokens.push({ kind: 'op', text: op, line })
        }
        if (tokens.length) out.push([...tokens, { kind: 'end', text: 'end of statement', line }])
      }
    })
  return out
}

/** Recursive descent, loosest first: `||`, `&&`, comparison, sum, product, sign, power, call or atom. */
function parser(tokens: Token[], known: ReadonlySet<string>) {
  let at = 0
  // Never past the end token, however a broken statement ends.
  const peek = () => tokens[Math.min(at, tokens.length - 1)] as Token
  const next = () => {
    const token = peek()
    at += 1
    return token
  }
  const fail = (message: string): never => {
    throw new SelectionError(message, peek().line)
  }
  const expect = (text: string) => {
    if (peek().text !== text) fail(`expected "${text}", found "${peek().text}"`)
    next()
  }

  const binary = (
    operand: () => Value,
    ops: Record<string, (a: number, b: number) => number>,
  ): (() => Value) => {
    return () => {
      let left = operand()
      while (peek().kind === 'op' && peek().text in ops) {
        const op = ops[next().text] as (a: number, b: number) => number
        const [l, r] = [left, operand()]
        left = (s) => op(l(s), r(s))
      }
      return left
    }
  }

  const atom = (): Value => {
    const token = next()
    if (token.kind === 'num') {
      const value = number(token.text)
      return () => value
    }
    if (token.text === '(') {
      const inner = expression()
      expect(')')
      return inner
    }
    if (token.kind === 'name') {
      if (peek().text === '(') return call(token.text)
      const name = token.text
      if (!known.has(name)) fail(`no metric named "${name}"${meant(name, known)}`)
      return (s) => s[name] ?? Number.NaN
    }
    return fail(`expected a number, metric or "(", found "${token.text}"`)
  }

  const call = (name: string): Value => {
    const found = FUNCTIONS[name]
    if (!found) return fail(`no function named "${name}"${meant(name, Object.keys(FUNCTIONS))}`)
    const [least, most, fn] = found
    expect('(')
    const args: Value[] = []
    if (peek().text !== ')') {
      args.push(expression())
      while (peek().text === ',') {
        next()
        args.push(expression())
      }
    }
    expect(')')
    if (args.length < least || args.length > most)
      fail(
        `${name} takes ${least === most ? least : `at least ${least}`} argument${least === 1 ? '' : 's'}`,
      )
    return (s) => fn(...args.map((a) => a(s)))
  }

  // Right-associative, as `2 ^ 3 ^ 2` is 2 ^ 9, and above a sign: `-2 ^ 2` is -4.
  const power = (): Value => {
    const base = atom()
    if (peek().text !== '^') return base
    next()
    const exponent = unary()
    return (s) => base(s) ** exponent(s)
  }

  const unary = (): Value => {
    if (peek().text === '!') {
      next()
      const inner = unary()
      return (s) => not(inner(s))
    }
    if (peek().text === '-') {
      next()
      const inner = unary()
      return (s) => -inner(s)
    }
    if (peek().text === '+') next()
    return power()
  }

  // A ratio over nought has no answer: NaN leaves the Alpha out, where Infinity would pass.
  const product = binary(unary, {
    '*': (a, b) => a * b,
    '/': (a, b) => (b === 0 ? Number.NaN : a / b),
  })
  const sum = binary(product, { '+': (a, b) => a + b, '-': (a, b) => a - b })
  const comparison = binary(sum, COMPARISONS)
  const both = binary(comparison, { '&&': and })
  const expression = binary(both, { '||': or })

  return { peek, next, fail, expression }
}

/** Throws a {@link SelectionError} naming the line at fault. `metrics` are the names allowed. */
export function compile(text: string, metrics: readonly string[]): Selection {
  const known = new Set(metrics)
  const named: { name: string; value: Value }[] = []
  const conditions: Value[] = []
  for (const tokens of statements(text)) {
    const p = parser(tokens, known)
    const [first, second] = tokens
    let target: string | undefined
    if (first?.kind === 'name' && second?.text === '=') {
      if (first.text in FUNCTIONS) p.fail(`"${first.text}" is a function`)
      target = first.text
      p.next()
      p.next()
    }
    const value = p.expression()
    if (p.peek().kind !== 'end') p.fail(`unexpected "${p.peek().text}"`)
    if (target === undefined) conditions.push(value)
    else {
      named.push({ name: target, value })
      known.add(target)
    }
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
