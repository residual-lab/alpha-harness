/**
 * Fast Expression, read in the browser with the backend's own grammar: `crates/fastexpr`,
 * built to WebAssembly and checked against `labs/fastexpr.py` on every Alpha in the vault.
 * A message is word for word the backend's. Loaded before any module that imports it runs, so
 * every call is synchronous.
 */

import url from './fastexpr.wasm?url'

export interface TreeNode {
  kind: 'num' | 'str' | 'name' | 'call' | 'unary' | 'binary' | 'ternary' | 'assign' | 'seq'
  value: string
  args: TreeNode[]
  kwargs: { name: string; value: TreeNode }[]
  /** Where its own token starts in the text read, in UTF-16 units: a call's name, an
   *  operator, a leaf. Absent on a node built here. */
  at?: number
}

/** A tree, or the backend's message and where in the text it points. */
export type Read = { tree: TreeNode } | { error: string; at: number }

const KINDS = ['num', 'str', 'name', 'op', 'comment', 'invalid'] as const

export type Token = [kind: (typeof KINDS)[number], from: number, to: number]

interface Core {
  memory: WebAssembly.Memory
  allocate: (length: number) => number
  release: (at: number, length: number) => void
  parse: (at: number, length: number, selection: number) => number
  render: (at: number, length: number, lines: number) => number
  tokens: (at: number, length: number) => number
}

// The page fetches it rather than streaming it, so the server's content type never matters.
const { instance } = await WebAssembly.instantiate(await (await fetch(url)).arrayBuffer())
const core = instance.exports as unknown as Core
const encoder = new TextEncoder()
const decoder = new TextDecoder()

/** The text in, the answer out: a little-endian length, then its bytes, copied out (and so
 *  aligned) before both buffers are given back. */
function call(fn: 'parse' | 'render' | 'tokens', text: string, flag = 0): ArrayBuffer {
  const bytes = encoder.encode(text)
  const at = core.allocate(bytes.length)
  new Uint8Array(core.memory.buffer, at, bytes.length).set(bytes)
  const out = core[fn](at, bytes.length, flag)
  core.release(at, bytes.length)
  const length = new DataView(core.memory.buffer).getUint32(out, true)
  const answer = core.memory.buffer.slice(out + 4, out + 4 + length)
  core.release(out, length + 4)
  return answer
}

const json = (fn: 'parse' | 'render', text: string, flag: number): unknown =>
  JSON.parse(decoder.decode(call(fn, text, flag)))

/** A Fast Expression as a tree. */
export const parse = (text: string) => json('parse', text, 0) as Read

/** A Selection Expression: Fast Expression over an Alpha's metrics, where `25%` and `5bps` are
 *  numbers, a line break ends a statement and a sign may be `+`. */
export const parseSelection = (text: string) => json('parse', text, 1) as Read

/** Every token of any text, comments and what cannot be read included: never fails. */
export function tokens(text: string): Token[] {
  const words = new Uint32Array(call('tokens', text))
  const found: Token[] = []
  for (let i = 0; i < words.length; i += 3)
    found.push([
      KINDS[words[i] as number] ?? 'invalid',
      words[i + 1] as number,
      words[i + 2] as number,
    ])
  return found
}

/** The tree with every operator's inputs bracketed, which always reads back as this tree. A
 *  missing input is `...`, Template Lab's empty one. */
function bracketed(node: TreeNode): string {
  const input = (i: number) => `(${bracketed(node.args[i] ?? HOLE)})`
  switch (node.kind) {
    case 'num':
    case 'str':
    case 'name':
      return node.value
    case 'call':
      return `${node.value}(${[
        ...node.args.map(bracketed),
        ...node.kwargs.map((k) => `${k.name}=${bracketed(k.value)}`),
      ].join(', ')})`
    case 'unary':
      return node.value + input(0)
    case 'binary':
      return `${input(0)} ${node.value} ${input(1)}`
    case 'ternary':
      return `${input(0)} ? ${input(1)} : ${input(2)}`
    case 'assign':
      return `${node.value} = ${bracketed(node.args[0] ?? HOLE)}`
    case 'seq':
      return node.args.map(bracketed).join(';\n')
  }
}

const HOLE: TreeNode = { kind: 'name', value: '...', args: [], kwargs: [] }

/** A tree as text, the way the backend writes it: bracketed only where the grammar needs it,
 *  with each statement on its own line. */
export function write(tree: TreeNode): string {
  const text = bracketed(tree)
  const written = json('render', text, 1) as { text: string } | { error: string }
  return 'text' in written ? written.text : text
}
