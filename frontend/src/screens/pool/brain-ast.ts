/** Splitting a Fast Expression into the pieces the Alpha inspector colours. */

export type AstKind = 'operator' | 'field' | 'number' | 'punctuation' | 'other'

export interface AstToken {
  text: string
  kind: AstKind
}

// The number branch takes an exponent too, so `1e-5` stays one token instead of leaving a
// stray `e` to be coloured as a data field.
const TOKEN =
  /([a-zA-Z_][a-zA-Z0-9_]*(?=\s*\())|((?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)|([a-zA-Z_][a-zA-Z0-9_]*)|([(),])|(\s+)|([^a-zA-Z0-9_\s(),.]+|\.)/g

/** Every character of `expr`, in order: a name followed by `(` is an operator. */
export function tokenizeBrainAst(expr: string): AstToken[] {
  const tokens: AstToken[] = []
  for (const [full, op, num, id, punct] of expr.matchAll(TOKEN)) {
    if (op) tokens.push({ text: op, kind: 'operator' })
    else if (num) tokens.push({ text: num, kind: 'number' })
    else if (punct) tokens.push({ text: punct, kind: 'punctuation' })
    else if (id) tokens.push({ text: id, kind: 'field' })
    else tokens.push({ text: full, kind: 'other' })
  }
  return tokens.length > 0 ? tokens : [{ text: expr, kind: 'other' }]
}
