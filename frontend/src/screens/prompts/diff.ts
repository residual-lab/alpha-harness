/**
 * A word-by-word diff of two prompts. Whitespace is kept as its own token, so the text puts
 * itself back together with its line breaks; only words are marked as added or removed.
 */

export interface DiffPart {
  op: 'same' | 'add' | 'del'
  text: string
}

/** Past this many LCS cells the comparison would stall typing, so it is not attempted. */
const MAX_CELLS = 4_000_000

const tokens = (text: string) => text.match(/\s+|\S+/g) ?? []

export function diffWords(before: string, after: string): DiffPart[] | null {
  const a = tokens(before)
  const b = tokens(after)
  // An edit is usually local: the shared head and tail need no table.
  let head = 0
  while (head < a.length && head < b.length && a[head] === b[head]) head++
  let tail = 0
  while (
    tail < a.length - head &&
    tail < b.length - head &&
    a[a.length - 1 - tail] === b[b.length - 1 - tail]
  )
    tail++
  const x = a.slice(head, a.length - tail)
  const y = b.slice(head, b.length - tail)
  const n = x.length
  const m = y.length
  if ((n + 1) * (m + 1) > MAX_CELLS) return null

  // lcs[i * w + j]: the longest common run of x[i:] and y[j:].
  const w = m + 1
  const lcs = new Uint32Array((n + 1) * w)
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      lcs[i * w + j] =
        x[i] === y[j]
          ? (lcs[(i + 1) * w + j + 1] ?? 0) + 1
          : Math.max(lcs[(i + 1) * w + j] ?? 0, lcs[i * w + j + 1] ?? 0)

  const parts: DiffPart[] = []
  const push = (op: DiffPart['op'], text: string) => {
    // A dropped run of spaces is noise, not a change worth marking.
    if (op === 'del' && !text.trim()) return
    const last = parts.at(-1)
    if (last?.op === op) last.text += text
    else parts.push({ op, text })
  }
  if (head) push('same', a.slice(0, head).join(''))
  let i = 0
  let j = 0
  while (i < n || j < m) {
    if (i < n && j < m && x[i] === y[j]) {
      push('same', x[i++] as string)
      j++
    } else if (j < m && (i === n || (lcs[i * w + j + 1] ?? 0) >= (lcs[(i + 1) * w + j] ?? 0))) {
      push('add', y[j++] as string)
    } else {
      push('del', x[i++] as string)
    }
  }
  if (tail) push('same', a.slice(a.length - tail).join(''))
  return parts
}

/** Words added and removed, for the summary above a diff. */
export function diffCounts(parts: DiffPart[]) {
  const words = (t: string) => t.split(/\s+/).filter(Boolean).length
  let added = 0
  let removed = 0
  for (const p of parts) {
    if (p.op === 'add') added += words(p.text)
    if (p.op === 'del') removed += words(p.text)
  }
  return { added, removed }
}
