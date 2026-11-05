/**
 * The Blocks view: the template the Code view types, drawn as nested blocks. It is read with
 * the same grammar every task runs on and written back as text, so the two never disagree.
 * Click an empty input to fill it, or a block to wrap, replace or remove it.
 */

import { Popover } from '@base-ui/react/popover'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Command } from 'cmdk'
import { CodeIcon, EraserIcon, PlusIcon, Redo2Icon, SearchIcon, Undo2Icon } from 'lucide-react'
import { createContext, type ReactNode, use, useState } from 'react'
import { catalog } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { MOD } from '@/shell/desktop'
import { Button, Notice, Segmented, Skeleton } from '@/ui/kit'
import { type TreeNode, templateLab } from './api'
import { GROUPS, inputsOf, type OperatorDoc, WORDS } from './fast-expression'

// ── The tree ───────────────────────────────────────────────────────────────────────────

const HOLE = '...'
type Path = readonly number[]

const leaf = (kind: 'num' | 'str' | 'name', value: string): TreeNode => ({
  kind,
  value,
  args: [],
  kwargs: [],
})
const hole = () => leaf('name', HOLE)
const isHole = (n: TreeNode) => n.kind === 'name' && n.value === HOLE
const isLeaf = (n: TreeNode) => n.kind === 'num' || n.kind === 'str' || n.kind === 'name'
const argOf = (n: TreeNode, i: number) => n.args[i] ?? hole()

/** A call's options are addressed after its inputs: index `args.length + k` is option `k`. */
function childAt(node: TreeNode, index: number): TreeNode | undefined {
  return node.args[index] ?? node.kwargs[index - node.args.length]?.value
}

function put(root: TreeNode, path: Path, next: TreeNode): TreeNode {
  const [index, ...rest] = path
  if (index === undefined) return next
  const child = childAt(root, index)
  if (!child) return root
  if (index < root.args.length) {
    const args = root.args.slice()
    args[index] = put(child, rest, next)
    return { ...root, args }
  }
  const kwargs = root.kwargs.map((k, i) =>
    i === index - root.args.length ? { ...k, value: put(child, rest, next) } : k,
  )
  return { ...root, kwargs }
}

const PRECEDENCE: Record<string, number> = {
  '||': 2,
  '&&': 3,
  '==': 4,
  '!=': 4,
  '<': 4,
  '<=': 4,
  '>': 4,
  '>=': 4,
  '+': 5,
  '-': 5,
  '*': 6,
  '/': 6,
  '^': 7,
}
const TERNARY = 1
const UNARY = 8

function precedence(n: TreeNode): number {
  if (n.kind === 'binary') return PRECEDENCE[n.value] ?? 0
  if (n.kind === 'unary') return UNARY
  if (n.kind === 'ternary') return TERNARY
  if (n.kind === 'assign' || n.kind === 'seq') return 0
  return 9
}

const wrapped = (n: TreeNode, parens: boolean) => (parens ? `(${write(n)})` : write(n))

/** The tree as text, bracketed exactly where the grammar needs it. */
export function write(n: TreeNode): string {
  switch (n.kind) {
    case 'num':
    case 'str':
    case 'name':
      return n.value
    case 'call':
      return `${n.value}(${[
        ...n.args.map(write),
        ...n.kwargs.map((k) => `${k.name}=${write(k.value)}`),
      ].join(', ')})`
    case 'unary': {
      const child = argOf(n, 0)
      return n.value + wrapped(child, precedence(child) < UNARY)
    }
    case 'binary': {
      const own = precedence(n)
      const [left, right] = [argOf(n, 0), argOf(n, 1)]
      // `^` binds to the right; every other operator to the left.
      const power = n.value === '^'
      const l = power ? precedence(left) <= own : precedence(left) < own
      const r = power ? precedence(right) < own : precedence(right) <= own
      return `${wrapped(left, l)} ${n.value} ${wrapped(right, r)}`
    }
    case 'ternary': {
      const test = argOf(n, 0)
      return `${wrapped(test, precedence(test) <= TERNARY)} ? ${write(argOf(n, 1))} : ${write(argOf(n, 2))}`
    }
    case 'assign':
      return `${n.value} = ${write(argOf(n, 0))}`
    case 'seq':
      return n.args.map(write).join(';\n')
  }
}

// ── What can be placed ─────────────────────────────────────────────────────────────────

type Pick =
  | { kind: 'call'; name: string; inputs: number }
  | { kind: 'binary'; symbol: string }
  | { kind: 'ternary' }
  | { kind: 'negate' }
  | { kind: 'leaf'; node: TreeNode }

/** An operator wraps what was there as its first input; anything else replaces it. */
function placed(pick: Pick, current: TreeNode): TreeNode {
  const first = current
  const holes = (count: number) => Array.from({ length: count }, hole)
  switch (pick.kind) {
    case 'call':
      return {
        kind: 'call',
        value: pick.name,
        args: [first, ...holes(Math.max(0, pick.inputs - 1))],
        kwargs: [],
      }
    case 'binary':
      return { kind: 'binary', value: pick.symbol, args: [first, hole()], kwargs: [] }
    case 'ternary':
      return { kind: 'ternary', value: '', args: [first, hole(), hole()], kwargs: [] }
    case 'negate':
      return { kind: 'unary', value: '-', args: [first], kwargs: [] }
    case 'leaf':
      return pick.node
  }
}

/**
 * `pick` in place of the operator `current`, on the same inputs: `ts_rank(close, 20)` becomes
 * `ts_zscore(close, 20)`. Inputs past the new one's count are dropped, missing ones left empty,
 * and options go: they belong to the operator being replaced.
 */
function swapped(pick: Pick, current: TreeNode): TreeNode {
  const take = (count: number) => Array.from({ length: count }, (_, i) => current.args[i] ?? hole())
  switch (pick.kind) {
    case 'call':
      return { kind: 'call', value: pick.name, args: take(pick.inputs), kwargs: [] }
    case 'binary':
      return { kind: 'binary', value: pick.symbol, args: take(2), kwargs: [] }
    case 'ternary':
      return { kind: 'ternary', value: '', args: take(3), kwargs: [] }
    case 'negate':
      return { kind: 'unary', value: '-', args: take(1), kwargs: [] }
    case 'leaf':
      return pick.node
  }
}

const OPERATOR_KINDS: TreeNode['kind'][] = ['call', 'binary', 'unary', 'ternary']

const ARITHMETIC = ['+', '-', '*', '/', '^']
const LOGICAL = ['>', '<', '>=', '<=', '==', '!=', '&&', '||']
const NUMBER = /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/
const NAME = /^\$?[A-Za-z_]\w*$/

function numberNode(text: string): TreeNode {
  return text.startsWith('-')
    ? { kind: 'unary', value: '-', args: [leaf('num', text.slice(1))], kwargs: [] }
    : leaf('num', text)
}

/** Every name the tree uses as `kind` with a `value` passing `keep`. */
function namesOf(n: TreeNode, kind: TreeNode['kind'], keep: (v: string) => boolean): string[] {
  const own = n.kind === kind && keep(n.value) ? [n.value] : []
  return [
    ...own,
    ...n.args.flatMap((a) => namesOf(a, kind, keep)),
    ...n.kwargs.flatMap((k) => namesOf(k.value, kind, keep)),
  ]
}

// ── Context ────────────────────────────────────────────────────────────────────────────

interface Ctx {
  root: TreeNode
  change: (next: TreeNode) => void
  operators: Map<string, OperatorDoc>
  /** `$names` without the `$`. */
  variables: string[]
  scope: Scope | null
}

const BlocksContext = createContext<Ctx | null>(null)

function useBlocks(): Ctx {
  const ctx = use(BlocksContext)
  if (!ctx) throw new Error('Template blocks must render inside the Blocks view.')
  return ctx
}

/** Muted Scratch-style tints, one per operator category (DESIGN.md category tokens). */
const CATEGORY: Record<string, string> = {
  'Cross Sectional': 'bg-category-cross-sectional border-category-cross-sectional-edge',
  'Time Series': 'bg-category-time-series border-category-time-series-edge',
  Group: 'bg-category-group border-category-group-edge',
  Arithmetic: 'bg-category-arithmetic border-category-arithmetic-edge',
  Logical: 'bg-category-logical border-category-logical-edge',
  Transformational: 'bg-category-transformational border-category-transformational-edge',
}

// ── The view ───────────────────────────────────────────────────────────────────────────

const COMMENT = /\/\*|\/\/|#/

export function TemplateBlocks({
  text,
  onChange,
  reference,
  variables,
  scope,
  onCode,
}: {
  text: string
  onChange: (text: string) => void
  reference: OperatorDoc[]
  variables: string[]
  scope: Scope | null
  onCode: () => void
}) {
  const blank = text.trim() === ''
  const read = useQuery({
    queryKey: ['template-lab', 'tree', text],
    queryFn: ({ signal }) => templateLab.tree(text, signal),
    enabled: !blank,
    staleTime: Number.POSITIVE_INFINITY,
    placeholderData: keepPreviousData,
  })
  // The tree just built here, shown at once rather than after the text is read back.
  const [mine, setMine] = useState<{ text: string; tree: TreeNode } | null>(null)
  const [history, setHistory] = useState<{ before: string; after: string }[]>([])
  const [undone, setUndone] = useState<{ before: string; after: string }[]>([])
  const last = history.at(-1)
  const next = undone.at(-1)
  // Only while the text is still what the step left: an edit in Code since makes it stale.
  const canUndo = last?.after === text
  const canRedo = next?.before === text

  const fresh = read.data && !read.isPlaceholderData ? read.data : undefined
  const root = mine?.text === text ? mine.tree : blank ? hole() : fresh?.tree
  const operators = new Map(reference.map((op) => [op.name, op]))

  const change = (next: TreeNode) => {
    const written = isHole(next) ? '' : write(next)
    if (written === text) return
    setMine({ text: written, tree: next })
    setHistory((h) => [...h.slice(-49), { before: text, after: written }])
    setUndone([])
    onChange(written)
  }
  const undo = () => {
    if (!last || !canUndo) return
    setHistory((h) => h.slice(0, -1))
    setUndone((u) => [...u, last])
    onChange(last.before)
  }
  const redo = () => {
    if (!next || !canRedo) return
    setUndone((u) => u.slice(0, -1))
    setHistory((h) => [...h, next])
    onChange(next.after)
  }

  if (!root) {
    if (fresh?.problem)
      return (
        <Notice
          tone="warn"
          title="Blocks can't show this template yet"
          action={
            <Button size="sm" onClick={onCode}>
              <CodeIcon />
              Fix in Code
            </Button>
          }
        >
          {fresh.problem}
        </Notice>
      )
    if (read.isError) return <Notice tone="error" title="Could not read this template" />
    return <Skeleton className="h-40" />
  }

  const statements = root.kind === 'seq' ? root.args : [root]
  // A named step before the last line, which stays the Alpha: `step1 = …;`.
  const addStep = () => {
    const taken = new Set(namesOf(root, 'assign', () => true))
    let n = 1
    while (taken.has(`step${n}`)) n += 1
    const step: TreeNode = { kind: 'assign', value: `step${n}`, args: [hole()], kwargs: [] }
    const alpha = statements.at(-1) ?? hole()
    change({ kind: 'seq', value: '', args: [...statements.slice(0, -1), step, alpha], kwargs: [] })
  }
  // Strings may hold `#`; only code outside them counts.
  const commented = COMMENT.test(text.replace(/"[^"]*"|'[^']*'/g, ''))

  return (
    <BlocksContext value={{ root, change, operators, variables, scope }}>
      {/* ⌘Z and ⇧⌘Z (Ctrl on Windows) while working in the blocks; a text box keeps its own. */}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: listens for keys from the blocks inside */}
      <div
        className="flex flex-col gap-2"
        onKeyDown={(e) => {
          if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== 'z') return
          if ((e.target as HTMLElement).closest('input, textarea')) return
          e.preventDefault()
          if (e.shiftKey) redo()
          else undo()
        }}
      >
        <div className="flex flex-wrap items-center justify-between gap-1">
          <Button size="sm" variant="secondary" onClick={addStep}>
            <PlusIcon />
            Add Step
          </Button>
          <span className="flex flex-wrap items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              disabled={!canUndo}
              title={`Undo (${MOD}Z)`}
              onClick={undo}
            >
              <Undo2Icon />
              Undo
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={!canRedo}
              title={`Redo (⇧${MOD}Z)`}
              onClick={redo}
            >
              <Redo2Icon />
              Redo
            </Button>
            <Button size="sm" variant="ghost" disabled={blank} onClick={() => change(hole())}>
              <EraserIcon />
              Clear
            </Button>
          </span>
        </div>
        {commented && (
          <Notice tone="info" title="A change here drops this template's comments">
            Blocks show the expression only. Keep the comments by editing in Code.
          </Notice>
        )}
        <div className="flex min-h-40 min-w-0 flex-col gap-2 overflow-auto rounded-lg border border-hairline bg-canvas p-4">
          {statements.map((statement, i) => {
            const path = root.kind === 'seq' ? [i] : []
            const lastLine = i === statements.length - 1
            return (
              <div key={i} className="flex min-w-0 flex-wrap items-center gap-1.5">
                {statement.kind === 'assign' ? (
                  <>
                    <span className="num text-body text-ink">{statement.value}</span>
                    <Punct text="=" />
                    <NodeView node={argOf(statement, 0)} path={[...path, 0]} hint="Input" />
                  </>
                ) : (
                  <NodeView
                    node={statement}
                    path={path}
                    hint={statements.length === 1 ? 'Click to choose a block' : 'Input'}
                    root={statements.length === 1}
                  />
                )}
                {!lastLine && <Punct text=";" />}
              </div>
            )
          })}
        </div>
      </div>
    </BlocksContext>
  )
}

function NodeView({
  node,
  path,
  hint,
  root = false,
}: {
  node: TreeNode
  path: Path
  hint: string
  root?: boolean
}) {
  if (isHole(node)) return <EmptySlot path={path} hint={hint} root={root} />
  if (node.kind === 'call') return <CallBlock node={node} path={path} />
  if (isLeaf(node)) return <Leaf node={node} path={path} />
  return <ExpressionBlock node={node} path={path} />
}

function Floating({
  open,
  onOpenChange,
  trigger,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  trigger: ReactNode
  children: ReactNode
}) {
  return (
    <Popover.Root open={open} onOpenChange={onOpenChange}>
      {trigger}
      <Popover.Portal>
        <Popover.Positioner sideOffset={6} align="start" className="z-50">
          <Popover.Popup className="max-w-[calc(100vw-2rem)] overflow-hidden rounded-md border border-hairline-strong bg-surface-3 shadow-float outline-none">
            {children}
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  )
}

function EmptySlot({ path, hint, root }: { path: Path; hint: string; root: boolean }) {
  const ctx = useBlocks()
  const [open, setOpen] = useState(false)
  return (
    <Floating
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Popover.Trigger
          className={cn(
            'inline-flex items-center justify-center border border-dashed border-hairline-strong text-ink-subtle transition-colors hover:border-ink-tertiary hover:text-ink',
            root ? 'min-h-32 w-full rounded-lg px-6 text-body' : 'h-6 min-w-16 rounded-xs px-3',
            !root && 'num text-body-compact',
          )}
        >
          {hint}
        </Popover.Trigger>
      }
    >
      <BlockSearch
        onPick={(pick) => {
          ctx.change(put(ctx.root, path, placed(pick, hole())))
          setOpen(false)
        }}
      />
    </Floating>
  )
}

/** A block's name, which opens what can be done to the block. */
function Handle({
  node,
  path,
  label,
  title,
  className,
}: {
  node: TreeNode
  path: Path
  label: string
  title?: string | undefined
  className?: string
}) {
  const ctx = useBlocks()
  const [open, setOpen] = useState(false)
  const done = (next: TreeNode) => {
    ctx.change(put(ctx.root, path, next))
    setOpen(false)
  }
  const kept = node.args[0]
  // On an operator, swapping it for another is the usual edit; wrapping is one click away.
  const operator = OPERATOR_KINDS.includes(node.kind)
  const [mode, setMode] = useState<'replace' | 'wrap'>('replace')
  const replacing = operator && mode === 'replace'
  return (
    <Floating
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Popover.Trigger
          {...(title ? { title } : {})}
          className={cn(
            'num min-w-0 truncate rounded-sm px-1 py-0.5 text-left text-body hover:bg-surface-3',
            className,
          )}
        >
          {label}
        </Popover.Trigger>
      }
    >
      <div className="flex w-80 max-w-full flex-col">
        <div className="flex flex-col gap-1.5 border-b border-hairline px-3 pt-2 pb-1.5">
          {operator && (
            <Segmented
              label="What an operator picked here does"
              value={mode}
              onChange={setMode}
              items={[
                { value: 'replace', label: 'Replace Operator' },
                { value: 'wrap', label: 'Wrap in Operator' },
              ]}
            />
          )}
          <p className="text-caption text-ink-subtle">
            {replacing
              ? 'An operator takes this one’s place, on the same inputs; anything else replaces the block.'
              : 'An operator wraps this block; anything else replaces it.'}
          </p>
        </div>
        <BlockSearch
          onPick={(pick) => done(replacing ? swapped(pick, node) : placed(pick, node))}
        />
        <div className="flex flex-wrap gap-2 border-t border-hairline p-2">
          {node.kind === 'call' && (
            <Button
              size="sm"
              onClick={() => done({ ...node, args: [...node.args, hole()] })}
              title="For operators that take any number of inputs"
            >
              Add Input
            </Button>
          )}
          {kept && !isLeaf(node) && (
            <Button size="sm" onClick={() => done(kept)}>
              Remove Block, Keep Input
            </Button>
          )}
          <Button size="sm" variant="danger" onClick={() => done(hole())}>
            Remove
          </Button>
        </div>
      </div>
    </Floating>
  )
}

function Punct({ text, className }: { text: string; className?: string }) {
  return <span className={cn('num text-body text-ink-subtle select-none', className)}>{text}</span>
}

function CallBlock({ node, path }: { node: TreeNode; path: Path }) {
  const ctx = useBlocks()
  const op = ctx.operators.get(node.value)
  const chosen = node.value.startsWith('$')
  const names = op ? inputsOf(op) : []
  const inputs = [
    ...node.args.map((arg, i) => ({ key: `${i}`, label: null, node: arg, hint: names[i] })),
    ...node.kwargs.map((k, i) => ({
      key: `${node.args.length + i}`,
      label: k.name,
      node: k.value,
      hint: k.name,
    })),
  ]
  // Only leaves inside: the whole call reads on one line, like `ts_rank(close, 20)`.
  const inline = inputs.every((x) => isLeaf(x.node))
  const tint = op ? CATEGORY[op.category] : undefined
  const input = (x: (typeof inputs)[number], i: number) => (
    <span key={x.key} className="flex min-w-0 items-center gap-1">
      {x.label && <span className="num text-body-compact text-ink-subtle">{x.label}=</span>}
      <NodeView node={x.node} path={[...path, Number(x.key)]} hint={x.hint ?? 'Input'} />
      {(i < inputs.length - 1 || inline) && <Punct text={i < inputs.length - 1 ? ',' : ')'} />}
    </span>
  )

  return (
    <div
      className={cn(
        'flex w-fit max-w-full min-w-0 flex-col gap-1 rounded-lg border px-1.5 py-1.5',
        tint ??
          (chosen ? 'border-hairline-strong bg-surface-2' : 'border-pnl-negative-dim bg-surface-2'),
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-1">
        <span className="flex min-w-0 items-center">
          <Handle
            node={node}
            path={path}
            label={node.value}
            title={
              op
                ? op.definition
                : chosen
                  ? 'A variable: the search chooses this operator'
                  : `${node.value} is not one of your operators`
            }
            className={chosen ? 'text-primary' : 'text-ink'}
          />
          <Punct text={inputs.length === 0 ? '()' : '('} />
        </span>
        {inline && inputs.map(input)}
      </div>
      {!inline && (
        <>
          <div className="ml-1 flex flex-col items-start gap-1 border-l border-hairline-strong pl-3">
            {inputs.map(input)}
          </div>
          <Punct text=")" className="pl-1" />
        </>
      )}
    </div>
  )
}

/** Arithmetic, comparisons, `-x` and `a ? b : c`: the operator sits between its inputs. */
function ExpressionBlock({ node, path }: { node: TreeNode; path: Path }) {
  const child = (i: number) => <NodeView node={argOf(node, i)} path={[...path, i]} hint="Input" />
  const handle = (label: string) => (
    <Handle node={node} path={path} label={label} className="text-ink" />
  )
  const logical = node.kind === 'ternary' || LOGICAL.includes(node.value) || node.value === '!'
  return (
    <div
      className={cn(
        'flex w-fit max-w-full min-w-0 flex-wrap items-center gap-1 rounded-lg border px-1.5 py-1.5',
        logical ? CATEGORY['Logical'] : CATEGORY['Arithmetic'],
      )}
    >
      {node.kind === 'unary' && (
        <>
          {handle(node.value)}
          {child(0)}
        </>
      )}
      {node.kind === 'binary' && (
        <>
          {child(0)}
          {handle(node.value)}
          {child(1)}
        </>
      )}
      {node.kind === 'ternary' && (
        <>
          {child(0)}
          {handle('?')}
          {child(1)}
          <Punct text=":" />
          {child(2)}
        </>
      )}
    </div>
  )
}

function Leaf({ node, path }: { node: TreeNode; path: Path }) {
  const variable = node.value.startsWith('$')
  const grouping = GROUPS.has(node.value)
  return (
    <span className="inline-flex h-6 w-fit items-center rounded-xs border border-hairline-strong bg-surface-3 transition-colors hover:bg-surface-4">
      <Handle
        node={node}
        path={path}
        label={node.value}
        title={
          variable
            ? 'A variable: the search chooses it for each Alpha'
            : grouping
              ? 'Grouping field'
              : undefined
        }
        className={cn(
          'text-body-compact hover:bg-transparent',
          variable ? 'text-primary' : node.kind === 'name' ? 'text-ink-muted' : 'text-ink',
        )}
      />
    </span>
  )
}

// ── Picking a block ────────────────────────────────────────────────────────────────────

const ITEM =
  'flex h-8 cursor-default items-center gap-2 rounded-sm px-2 text-body text-ink-muted select-none data-[selected=true]:bg-surface-4 data-[selected=true]:text-ink'

function BlockSearch({ onPick }: { onPick: (pick: Pick) => void }) {
  const ctx = useBlocks()
  const [search, setSearch] = useState('')
  const typed = search.trim()
  const fields = useQuery({
    queryKey: ['template-lab', 'block-fields', ctx.scope, typed],
    queryFn: () =>
      ctx.scope
        ? catalog.fields(ctx.scope, {
            search: typed,
            search_mode: 'text',
            sort_by: 'alpha_count',
            sort_desc: true,
            limit: 30,
          })
        : null,
    enabled: ctx.scope !== null && typed.length >= 2 && !typed.startsWith('$'),
    placeholderData: keepPreviousData,
  })

  const steps = [...new Set(namesOf(ctx.root, 'assign', () => true))]
  const calledVariables = new Set(namesOf(ctx.root, 'call', (v) => v.startsWith('$')))
  const byCategory = new Map<string, OperatorDoc[]>()
  for (const op of ctx.operators.values())
    byCategory.set(op.category, [...(byCategory.get(op.category) ?? []), op])

  const item = (key: string, label: ReactNode, pick: Pick, detail?: string, always = false) => (
    <Command.Item
      key={key}
      value={key}
      // Typed suggestions and catalog matches always pass the filter: they come from the search.
      keywords={always ? [search] : []}
      onSelect={() => onPick(pick)}
      className={ITEM}
    >
      <span className="num truncate">{label}</span>
      {detail && <span className="ml-auto truncate text-caption text-ink-subtle">{detail}</span>}
    </Command.Item>
  )
  const call = (name: string, inputs: number) => ({ kind: 'call' as const, name, inputs })
  const variableName = typed.startsWith('$') ? typed.slice(1) : null

  return (
    <Command
      loop
      className="flex w-80 max-w-full flex-col [&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1"
    >
      <div className="flex items-center gap-2 border-b border-hairline px-3 transition-colors focus-within:border-ink-subtle">
        <SearchIcon className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
        <Command.Input
          autoFocus
          value={search}
          onValueChange={setSearch}
          placeholder="Search blocks, or type a number or $name…"
          className="h-9 flex-1 bg-transparent text-body text-ink outline-none placeholder:text-ink-subtle"
        />
      </div>
      <Command.List className="max-h-80 overflow-y-auto p-1">
        <Command.Empty className="px-3 py-4 text-center text-body text-ink-subtle">
          Nothing matches.
        </Command.Empty>
        {NUMBER.test(typed) && (
          <Command.Group heading="Number">
            {item(`number:${typed}`, typed, { kind: 'leaf', node: numberNode(typed) }, '', true)}
          </Command.Group>
        )}
        {variableName !== null && NAME.test(typed) && (
          <Command.Group heading="New Variable">
            {item(`new:${typed}`, typed, { kind: 'leaf', node: leaf('name', typed) }, '', true)}
            {item(
              `new-op:${typed}`,
              `${typed}(…)`,
              call(typed, 1),
              'the search chooses an operator',
              true,
            )}
          </Command.Group>
        )}
        {ctx.variables.length > 0 && (
          <Command.Group heading="Variables">
            {ctx.variables.map((name) =>
              calledVariables.has(`$${name}`)
                ? item(`$${name}(`, `$${name}(…)`, call(`$${name}`, 1), 'operator')
                : item(`$${name}`, `$${name}`, { kind: 'leaf', node: leaf('name', `$${name}`) }),
            )}
          </Command.Group>
        )}
        {steps.length > 0 && (
          <Command.Group heading="Steps">
            {steps.map((name) =>
              item(`step:${name}`, name, { kind: 'leaf', node: leaf('name', name) }),
            )}
          </Command.Group>
        )}
        {[...byCategory.entries()].map(([category, ops]) => (
          <Command.Group key={category} heading={category}>
            {ops.map((op) =>
              item(op.name, op.name, call(op.name, Math.max(1, inputsOf(op).length))),
            )}
          </Command.Group>
        ))}
        <Command.Group heading="Arithmetic">
          {ARITHMETIC.map((symbol) =>
            item(`infix:${symbol}`, `… ${symbol} …`, { kind: 'binary', symbol }),
          )}
          {item('negate', '-…', { kind: 'negate' })}
        </Command.Group>
        <Command.Group heading="Logical">
          {LOGICAL.map((symbol) =>
            item(`infix:${symbol}`, `… ${symbol} …`, { kind: 'binary', symbol }),
          )}
          {item('ternary', '… ? … : …', { kind: 'ternary' }, 'if, then, else')}
        </Command.Group>
        <Command.Group heading="Grouping Fields">
          {[...GROUPS].map((name) =>
            item(`group:${name}`, name, { kind: 'leaf', node: leaf('name', name) }),
          )}
        </Command.Group>
        {(fields.data?.results.length ?? 0) > 0 && (
          <Command.Group heading="Data Fields">
            {fields.data?.results.map((field) =>
              item(
                `field:${field.field_id}`,
                field.field_id,
                { kind: 'leaf', node: leaf('name', field.field_id) },
                field.dataset_id ?? '',
                true,
              ),
            )}
          </Command.Group>
        )}
        {NAME.test(typed) && variableName === null && (
          <Command.Group heading="Name">
            {item(`name:${typed}`, typed, { kind: 'leaf', node: leaf('name', typed) }, '', true)}
          </Command.Group>
        )}
        <Command.Group heading="Values">
          {WORDS.map((word) =>
            item(`word:${word}`, word, { kind: 'leaf', node: leaf('name', word) }),
          )}
        </Command.Group>
      </Command.List>
    </Command>
  )
}
