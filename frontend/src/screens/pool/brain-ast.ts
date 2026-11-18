/** Splitting a Fast Expression into the pieces the Alpha inspector colours, as the grammar reads it. */

import { tokens } from '@/lib/fastexpr'

export type AstKind = 'operator' | 'field' | 'number' | 'punctuation' | 'other'

export interface AstToken {
  text: string
  kind: AstKind
}

/** Every character of `expr`, in order: a name followed by `(` is an operator. Comments,
 *  strings and the space between are `other`. */
export function tokenizeBrainAst(expr: string): AstToken[] {
  const found: AstToken[] = []
  const read = tokens(expr)
  let end = 0
  const add = (to: number, kind: AstKind) => {
    if (to > end) found.push({ text: expr.slice(end, to), kind })
    end = to
  }
  read.forEach(([kind, from, to], i) => {
    add(from, 'other')
    const text = expr.slice(from, to)
    const next = read[i + 1]
    const called = next?.[0] === 'op' && expr.slice(next[1], next[2]) === '('
    add(
      to,
      kind === 'name'
        ? called
          ? 'operator'
          : 'field'
        : kind === 'num'
          ? 'number'
          : kind === 'op' && '(),'.includes(text)
            ? 'punctuation'
            : 'other',
    )
  })
  add(expr.length, 'other')
  return found.length > 0 ? found : [{ text: expr, kind: 'other' }]
}
