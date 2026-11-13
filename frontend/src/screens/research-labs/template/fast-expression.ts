/**
 * Fast Expression for the template editor: a highlighter that knows a template's `$variables`,
 * `...` and grouping fields, the plain-text scans its completions read, and its checks. The
 * highlighter is CodeMirror's own line-at-a-time stream, which colours only what is on screen;
 * everything else reads the text with the grammar every task runs on.
 */

import { StreamLanguage } from '@codemirror/language'
import type { Diagnostic } from '@codemirror/lint'
import { Tag, tags as t } from '@lezer/highlight'
import { parse, tokens } from '@/lib/fastexpr'

/** An operator the account can call, as the editor documents it. */
export interface OperatorDoc {
  name: string
  category: string
  definition: string
  description: string
}

/** Grouping fields: BRAIN groups stocks by them, and never counts them as data. */
export const GROUPS = new Set([
  'market',
  'sector',
  'industry',
  'subindustry',
  'country',
  'exchange',
  'currency',
])
export const WORDS = ['true', 'false', 'nan']

export const variable = Tag.define()
export const hole = Tag.define()
export const group = Tag.define()
export const local = Tag.define()
export const brackets = [Tag.define(), Tag.define(), Tag.define()] as const

interface Reading {
  /** Brackets open here, for colouring each pair by its depth. */
  depth: number
  /** Inside a block comment that started on an earlier line. */
  comment: boolean
}

export const fastExpression = StreamLanguage.define<Reading>({
  name: 'fast-expression',
  startState: () => ({ depth: 0, comment: false }),
  token(stream, state) {
    if (state.comment) {
      state.comment = !stream.skipTo('*/')
      if (state.comment) stream.skipToEnd()
      else stream.match('*/')
      return 'comment'
    }
    if (stream.eatSpace()) return null
    if (stream.match('/*')) {
      state.comment = true
      return 'comment'
    }
    if (stream.match('//') || stream.eat('#')) {
      stream.skipToEnd()
      return 'comment'
    }
    if (stream.match('...')) return 'hole'
    if (stream.match(/^"[^"]*"?/) || stream.match(/^'[^']*'?/)) return 'string'
    if (stream.match(/^(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/)) return 'number'
    // Not `variable`: CodeMirror reads that old name as a plain variableName.
    if (stream.match(/^\$[A-Za-z_]\w*/)) return 'dollar'
    if (stream.match(/^[A-Za-z_]\w*/)) {
      const word = stream.current()
      if (stream.match(/^\s*\(/, false)) return 'call'
      if (WORDS.includes(word.toLowerCase())) return 'bool'
      if (GROUPS.has(word)) return 'group'
      // A name before `=` is a step being named, or inside brackets an option's name.
      if (stream.match(/^\s*=(?!=)/, false)) return state.depth > 0 ? 'option' : 'local'
      return 'field'
    }
    if (stream.match(/^[([{]/)) return `bracket${state.depth++ % 3}`
    if (stream.match(/^[)\]}]/)) {
      if (state.depth === 0) return 'invalid'
      state.depth -= 1
      return `bracket${state.depth % 3}`
    }
    if (stream.match(/^(?:&&|\|\||==|!=|<=|>=|[-+*/^<>!?:=])/)) return 'operator'
    if (stream.match(';')) {
      // A statement ends here, so a bracket it left open cannot colour the next one's names.
      state.depth = 0
      return 'punctuation'
    }
    if (stream.eat(',')) return 'punctuation'
    stream.next()
    return 'invalid'
  },
  tokenTable: {
    dollar: variable,
    hole,
    group,
    local,
    call: t.function(t.variableName),
    option: t.propertyName,
    field: t.variableName,
    bracket0: brackets[0],
    bracket1: brackets[1],
    bracket2: brackets[2],
  },
  languageData: {
    closeBrackets: { brackets: ['(', '[', '{', '"', "'"] },
    commentTokens: { line: '//', block: { open: '/*', close: '*/' } },
  },
})

/** The text with comments and strings blanked, every position and line break kept, so a scan
 *  reads only code and a statement still ends where its line does. One left open while it is
 *  typed runs to the end. */
export function code(text: string): string {
  let blank = ''
  let end = 0
  for (const [kind, from, to] of tokens(text)) {
    if (kind !== 'comment' && kind !== 'str') continue
    blank += text.slice(end, from) + text.slice(from, to).replace(/[^\n]/g, ' ')
    end = to
  }
  return blank + text.slice(end)
}

/** A name being assigned, read from where a statement starts. */
const ASSIGNED = /\s*([A-Za-z_]\w*)\s*=(?!=)/y

/**
 * The names the template's own lines define: `r = …;`. Only outside brackets, as the
 * highlighter reads them: inside a call spread over lines, `lag = 1` names an option.
 */
export function locals(blank: string): Set<string> {
  const found = new Set<string>()
  let depth = 0
  const statement = (at: number) => {
    if (depth > 0) return
    ASSIGNED.lastIndex = at
    const name = ASSIGNED.exec(blank)?.[1]
    if (name) found.add(name)
  }
  statement(0)
  for (let at = 0; at < blank.length; at++) {
    const ch = blank[at]
    if (ch === '(' || ch === '[' || ch === '{') depth += 1
    else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1)
    else if (ch === ';') {
      // A statement ends here, as the highlighter also reads it: an unclosed bracket stops.
      depth = 0
      statement(at + 1)
    } else if (ch === '\n') statement(at + 1)
  }
  return found
}

/** Every bare name, not a `$variable` and not an operator being called. */
export const NAMES = /(?<![$\w])[A-Za-z_]\w*(?!\w)(?!\s*\()/g

/** The inputs an operator needs, by the names its definition gives them: `x`, `d`. */
export function inputsOf(op: OperatorDoc): string[] {
  const first = op.definition.split(/\r?\n/)[0] ?? ''
  const open = first.indexOf('(')
  const params: string[] = []
  if (open >= 0) {
    let depth = 0
    let quoted = false
    let current = ''
    for (const ch of first.slice(open + 1)) {
      if ('"\'“”'.includes(ch)) quoted = !quoted
      else if (!quoted && ch === '(') depth += 1
      else if (!quoted && ch === ')') {
        if (depth === 0) break
        depth -= 1
      } else if (!quoted && depth === 0 && ch === ',') {
        params.push(current.trim())
        current = ''
        continue
      }
      current += ch
    }
    params.push(current.trim())
  }
  return params.filter((p) => p && !p.includes('=') && !p.includes('..'))
}

/** The inputs an operator needs, as a snippet: `ts_rank(${x}, ${d})`. */
export function snippetOf(op: OperatorDoc): string {
  const fields = inputsOf(op)
    .map((p) => `\${${p.replace(/[{}$\\]/g, '')}}`)
    .join(', ')
  return `${op.name}(${fields || `\${}`})`
}

/**
 * The preview's problems that name something in the text, by the exact words it writes them in:
 * a variable's own as `$name: …`, the rest as a sentence about a named operator or field. Any
 * other sentence has no place and stays below the editor.
 */
const SHAPES: { pattern: RegExp; call?: boolean }[] = [
  { pattern: /^(\$[A-Za-z_]\w*): (.+)$/ },
  { pattern: /^(\$[A-Za-z_]\w*) is chosen by the search/ },
  { pattern: /^([A-Za-z_]\w*) is not one of your operators\.$/, call: true },
  { pattern: /^([A-Za-z_]\w*) does not take \d+ inputs?\.$/, call: true },
  { pattern: /^([A-Za-z_]\w*) is not a data field in /, call: false },
]

/** The backend's words for a template its grammar cannot read, before what stopped it. */
const UNREAD = "The template can't be read"

/**
 * What the editor marks on the text: each `...` to fill, a bracket left open or closed twice,
 * an operator the account lacks (`operators`, once known), where the grammar stopped reading,
 * and the preview's `problems` that name something written.
 */
export function lint(
  text: string,
  operators: ReadonlySet<string>,
  problems: string[],
): Diagnostic[] {
  const read = tokens(text)
  const word = ([, from, to]: (typeof read)[number]) => text.slice(from, to)
  const found: Diagnostic[] = []
  for (const token of read)
    if (token[0] === 'name' && word(token) === '...')
      found.push({
        from: token[1],
        to: token[2],
        severity: 'warning',
        message: 'Write a signal here.',
      })
  const open: number[] = []
  for (const token of read) {
    if (token[0] !== 'op') continue
    if (word(token) === '(') open.push(token[1])
    else if (word(token) === ')' && open.pop() === undefined)
      found.push({
        from: token[1],
        to: token[2],
        severity: 'error',
        message: 'Nothing opens this bracket.',
      })
  }
  for (const at of open)
    found.push({
      from: at,
      to: at + 1,
      severity: 'error',
      message: 'This bracket is never closed.',
    })
  if (operators.size) {
    read.forEach((token, i) => {
      const name = word(token)
      const next = read[i + 1]
      const called = next?.[0] === 'op' && word(next) === '('
      // A name stuck to a `.` or a number is a typo already marked where reading stops.
      const stuck = /[\w$.]/.test(text[token[1] - 1] ?? '')
      if (
        token[0] === 'name' &&
        called &&
        !stuck &&
        /^[A-Za-z_]/.test(name) &&
        !operators.has(name)
      )
        found.push({
          from: token[1],
          to: token[2],
          severity: 'error',
          message: `${name} is not one of your operators.`,
        })
    })
  }

  // Read here rather than waiting on the preview, in the same words; its own copy is dropped.
  const parsed = parse(text)
  if ('error' in parsed && / at \d+/.test(parsed.error)) {
    const at = Math.min(parsed.at, Math.max(0, text.length - 1))
    const message = `${UNREAD}: ${parsed.error}`
    found.push({ from: at, to: Math.min(text.length, at + 1), severity: 'error', message })
  }
  const blank = code(text)
  for (const problem of problems) {
    if (problem.startsWith(UNREAD)) continue
    for (const shape of SHAPES) {
      const named = shape.pattern.exec(problem)
      const name = named?.[1]
      if (!name) continue
      const message = named[2] ?? problem
      const escaped = name.replace('$', '\\$')
      const where = name.startsWith('$')
        ? `${escaped}(?!\\w)`
        : `(?<![$\\w])${escaped}(?!\\w)${shape.call ? '(?=\\s*\\()' : '(?!\\s*\\()'}`
      for (const m of blank.matchAll(new RegExp(where, 'g')))
        found.push({ from: m.index, to: m.index + name.length, severity: 'error', message })
      break
    }
  }

  const seen = new Set<string>()
  return found.filter((d) => {
    const key = `${d.from}:${d.to}:${d.message}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}
