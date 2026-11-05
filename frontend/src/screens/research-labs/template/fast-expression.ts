/**
 * Fast Expression for the template editor: a tokenizer that knows a template's `$variables`,
 * `...` and grouping fields, and the plain-text scans its completions and checks read.
 */

import { StreamLanguage } from '@codemirror/language'
import { Tag, tags as t } from '@lezer/highlight'

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
 *  reads only code and a statement still ends where its line does. */
export function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?(?:\*\/|$)|\/\/[^\n]*|#[^\n]*|"[^"]*"?|'[^']*'?/g, (m) =>
    m.replace(/[^\n]/g, ' '),
  )
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
