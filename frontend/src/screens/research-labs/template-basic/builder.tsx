/**
 * The block builder: a palette of the account's blocks beside a canvas holding the
 * template's tree. Blocks are dragged onto slots, or placed by clicking a slot and picking
 * from a searchable list, so every change also works without dragging (and on touch).
 */

import { Popover } from '@base-ui/react/popover'
import { Command } from 'cmdk'
import { CheckIcon, EraserIcon, RefreshCwIcon, SearchIcon, Undo2Icon, XIcon } from 'lucide-react'
import {
  createContext,
  type DragEvent as ReactDragEvent,
  type ReactNode,
  use,
  useEffect,
  useRef,
  useState,
} from 'react'
import { cn } from '@/lib/cn'
import type { TemplateLabOptions } from '@/screens/research-labs/template-basic/api'
import {
  accepts,
  argumentRows,
  type Blocks,
  blockOf,
  canMove,
  choices,
  drop,
  firstHole,
  keyPath,
  move,
  nextTag,
  type OperatorNode,
  type Path,
  paletteSections,
  pathKey,
  put,
  retag,
  type Slot,
  type Socket,
  socketAt,
  type TemplateDoc,
  type TemplateNode,
  tagsOf,
  unwrap,
  type VariableNode,
} from '@/screens/research-labs/template-basic/tree'
import { Button, Checkbox, Chips, Disclosure, Fieldset, Input, Notice, Panel } from '@/ui/kit'
import { SplitPane } from '@/ui/panels'

const SOCKET_LABEL: Record<Socket, string> = {
  signal: 'Input',
  lookback: 'Lookback',
  group: 'Group',
}

/** Scratch-style muted colours, one per operator category: a tint fill and its edge (DESIGN.md category tokens). */
const CATEGORY: Record<string, { fill: string; edge: string }> = {
  'Cross Sectional': {
    fill: 'bg-category-cross-sectional',
    edge: 'border-category-cross-sectional-edge',
  },
  'Time Series': { fill: 'bg-category-time-series', edge: 'border-category-time-series-edge' },
  Group: { fill: 'bg-category-group', edge: 'border-category-group-edge' },
  Arithmetic: { fill: 'bg-category-arithmetic', edge: 'border-category-arithmetic-edge' },
  Logical: { fill: 'bg-category-logical', edge: 'border-category-logical-edge' },
  Transformational: {
    fill: 'bg-category-transformational',
    edge: 'border-category-transformational-edge',
  },
}

const tintOf = (category: string | undefined) => (category ? CATEGORY[category] : undefined)

// ── Dragging ────────────────────────────────────────────────────────────────────────────

type Payload =
  | { from: 'palette'; node: TemplateNode; label: string }
  | { from: 'canvas'; path: Path; label: string }

/**
 * Native drag and drop, which ignores touch — click-to-place is the path that works there. The
 * payload lives in React state rather than dataTransfer, since a drag never leaves the page.
 */
function useDragDrop(onDrop: (payload: Payload, target: string) => void) {
  const [dragging, setDragging] = useState<Payload | null>(null)
  const [over, setOver] = useState<string | null>(null)
  const dropped = useRef(onDrop)
  useEffect(() => {
    dropped.current = onDrop
  })

  const targetOf = (event: ReactDragEvent) =>
    (event.target as HTMLElement).closest<HTMLElement>('[data-drop]')?.dataset['drop'] ?? null
  const clear = () => {
    setDragging(null)
    setOver(null)
  }

  const begin = (event: ReactDragEvent, payload: Payload) => {
    // Nested blocks all hear the start; the innermost, which hears it first, is the one dragged.
    event.stopPropagation()
    if ((event.target as HTMLElement).closest('input, textarea')) {
      event.preventDefault()
      return
    }
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', payload.label) // Firefox starts no drag without data
    setDragging(payload)
  }

  const surface = {
    onDragOver: (event: ReactDragEvent) => {
      const key = targetOf(event)
      if (key !== null) {
        event.preventDefault()
        event.dataTransfer.dropEffect = 'move'
      }
      setOver(key)
    },
    onDragLeave: (event: ReactDragEvent) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOver(null)
    },
    onDrop: (event: ReactDragEvent) => {
      event.preventDefault()
      const key = targetOf(event)
      if (dragging && key) dropped.current(dragging, key)
      clear()
    },
    onDragEnd: clear,
  }

  return { dragging, over, begin, surface }
}

// ── Context ─────────────────────────────────────────────────────────────────────────────

interface Ctx {
  root: Slot
  blocks: Blocks
  options: TemplateLabOptions | undefined
  dragging: Payload | null
  over: string | null
  hoverTag: string | null
  setHoverTag: (tag: string | null) => void
  begin: (event: ReactDragEvent, payload: Payload) => void
  fits: (payload: Payload, path: Path) => boolean
  change: (root: Slot) => void
  place: (node: TemplateNode) => void
}

const BuilderContext = createContext<Ctx | null>(null)

function useBuilder(): Ctx {
  const ctx = use(BuilderContext)
  if (!ctx) throw new Error('Template blocks must render inside the builder.')
  return ctx
}

function useDropState(path: Path): 'over' | 'fits' | 'blocked' | null {
  const ctx = useBuilder()
  if (!ctx.dragging) return null
  if (!ctx.fits(ctx.dragging, path)) return 'blocked'
  return ctx.over === pathKey(path) ? 'over' : 'fits'
}

// ── The builder ─────────────────────────────────────────────────────────────────────────

export function Builder({
  doc,
  blocks,
  options,
  problems,
  skeleton,
  canUndo,
  syncing,
  onChange,
  onUndo,
  onSync,
}: {
  doc: TemplateDoc
  blocks: Blocks
  options: TemplateLabOptions | undefined
  problems: string[]
  skeleton: string | undefined
  canUndo: boolean
  syncing: boolean
  onChange: (doc: TemplateDoc) => void
  onUndo: () => void
  onSync: () => void
}) {
  const root = doc.root
  const [hoverTag, setHoverTag] = useState<string | null>(null)
  const change = (next: Slot) => onChange({ version: 1, root: next })

  const fits = (payload: Payload, path: Path): boolean =>
    payload.from === 'palette'
      ? accepts(socketAt(root, path, blocks), payload.node, blocks)
      : // The same rule the move itself applies, so a drop the tree would refuse is drawn
        // as blocked rather than accepted and then quietly ignored.
        canMove(root, payload.path, path, blocks)

  const { dragging, over, begin, surface } = useDragDrop((payload, key) => {
    if (key === 'trash') {
      if (payload.from === 'canvas') change(put(root, payload.path, null))
      return
    }
    const path = keyPath(key)
    if (!fits(payload, path)) return
    if (payload.from === 'palette') {
      change(drop(root, path, payload.node, blocks))
      return
    }
    const next = move(root, payload.path, path, blocks)
    if (next !== undefined) change(next)
  })

  const place = (node: TemplateNode) => {
    const path = firstHole(root, node, blocks)
    if (path) change(drop(root, path, node, blocks))
    else if (node.kind === 'op') change(drop(root, [], node, blocks))
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        (event.target as HTMLElement | null)?.closest('input, textarea, [contenteditable="true"]')
      )
        return
      if ((event.metaKey || event.ctrlKey) && !event.shiftKey && event.key.toLowerCase() === 'z') {
        event.preventDefault()
        onUndo()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onUndo])

  const ctx: Ctx = {
    root,
    blocks,
    options,
    dragging,
    over,
    hoverTag,
    setHoverTag,
    begin,
    fits,
    change,
    place,
  }

  return (
    <BuilderContext value={ctx}>
      <Panel
        title="Blocks"
        actions={
          <>
            <Button size="sm" variant="ghost" disabled={!canUndo} onClick={onUndo}>
              <Undo2Icon />
              Undo
            </Button>
            <Button size="sm" variant="ghost" disabled={root === null} onClick={() => change(null)}>
              <EraserIcon />
              Clear
            </Button>
            <Button size="sm" variant="ghost" loading={syncing} onClick={onSync}>
              <RefreshCwIcon />
              Sync Operators
            </Button>
          </>
        }
      >
        <div
          className={cn('flex flex-col gap-3', dragging && 'cursor-grabbing select-none')}
          {...surface}
        >
          <SplitPane id="template-lab" first={{ default: 300, min: 220, max: 520 }}>
            <Palette />
            <div className="flex min-h-80 min-w-0 overflow-auto rounded-lg border border-hairline bg-canvas p-4">
              <SlotView slot={root} path={[]} />
            </div>
          </SplitPane>
          {problems.map((problem) => (
            <Notice key={problem} tone="error" title={problem} />
          ))}
          {skeleton && root !== null && (
            <Disclosure summary="Expression">
              <code className="num text-body-compact break-all text-ink">{skeleton}</code>
            </Disclosure>
          )}
        </div>
      </Panel>
    </BuilderContext>
  )
}

// ── Palette ─────────────────────────────────────────────────────────────────────────────

function Palette() {
  const ctx = useBuilder()
  const [search, setSearch] = useState('')
  const term = search.trim().toLowerCase()
  const sections = paletteSections(ctx.root, ctx.blocks, ctx.options)
    .map((section) => ({
      ...section,
      items: term
        ? section.items.filter((item) => item.label.toLowerCase().includes(term))
        : section.items,
    }))
    .filter((section) => section.items.length > 0)
  const removing = ctx.dragging?.from === 'canvas'

  return (
    <div
      data-drop="trash"
      className={cn(
        'flex h-full min-h-0 flex-col gap-3 rounded-lg border p-3 transition-colors',
        removing && ctx.over === 'trash'
          ? 'border-pnl-negative-dim bg-pnl-negative-tint'
          : removing
            ? 'border-hairline-strong'
            : 'border-hairline',
      )}
    >
      <Input
        placeholder="Search blocks"
        aria-label="Search blocks"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="-m-1 flex max-h-120 min-h-0 flex-col gap-3 overflow-y-auto p-1">
        {sections.map((section) => (
          <section key={section.heading} className="flex flex-col gap-1.5">
            <h3 className="text-caption font-medium text-ink-subtle">{section.heading}</h3>
            <div className="flex flex-wrap gap-1.5">
              {section.items.map((item) => {
                const tint =
                  item.node.kind === 'op'
                    ? tintOf(blockOf(ctx.blocks, item.node)?.category)
                    : undefined
                return (
                  <button
                    key={item.key}
                    type="button"
                    title={item.hint ?? item.label}
                    draggable
                    onDragStart={(event) =>
                      ctx.begin(event, {
                        from: 'palette',
                        node: item.node,
                        label: item.label,
                      })
                    }
                    onClick={() => ctx.place(item.node)}
                    className={cn(
                      'inline-flex h-7 max-w-full cursor-grab items-center border px-3 num text-body-compact text-ink-muted transition-colors select-none hover:text-ink',
                      tint
                        ? [tint.fill, tint.edge]
                        : 'border-hairline bg-surface-2 hover:border-hairline-strong',
                      item.node.kind === 'op' ? 'rounded-md' : 'rounded-xs',
                    )}
                  >
                    <span className="truncate">{item.label}</span>
                  </button>
                )
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}

// ── Canvas ──────────────────────────────────────────────────────────────────────────────

/** `suffix` is the punctuation that follows the slot in its parent's call: a comma, or closing brackets. */
function SlotView({ slot, path, suffix }: { slot: Slot; path: Path; suffix?: string | undefined }) {
  if (slot?.kind === 'op') return <OperatorBlock node={slot} path={path} suffix={suffix} />
  const view = slot === null ? <EmptySlot path={path} /> : <Leaf node={slot} path={path} />
  if (!suffix) return view
  return (
    <span className="flex items-center">
      {view}
      <Punct text={suffix} />
    </span>
  )
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

function EmptySlot({ path }: { path: Path }) {
  const ctx = useBuilder()
  const [open, setOpen] = useState(false)
  const socket = socketAt(ctx.root, path, ctx.blocks)
  const state = useDropState(path)
  const isRoot = path.length === 0

  return (
    <Floating
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Popover.Trigger
          data-drop={pathKey(path)}
          className={cn(
            'inline-flex items-center justify-center border border-dashed transition-colors',
            isRoot
              ? 'min-h-40 w-full rounded-lg px-6 text-body'
              : 'h-6 min-w-16 rounded-xs px-3 text-body-compact',
            state === 'over'
              ? 'border-primary bg-surface-3 text-ink'
              : state === 'fits'
                ? 'border-primary text-ink-subtle'
                : state === 'blocked'
                  ? 'border-hairline text-ink-subtle'
                  : 'border-hairline-strong text-ink-subtle hover:border-ink-tertiary hover:text-ink',
          )}
        >
          {isRoot ? 'Drag a block here, or click to choose one' : SOCKET_LABEL[socket]}
        </Popover.Trigger>
      }
    >
      <BlockSearch
        socket={socket}
        onPick={(node) => {
          ctx.change(drop(ctx.root, path, node, ctx.blocks))
          setOpen(false)
        }}
      />
    </Floating>
  )
}

function OperatorBlock({
  node,
  path,
  suffix = '',
}: {
  node: OperatorNode
  path: Path
  suffix?: string | undefined
}) {
  const ctx = useBuilder()
  const [open, setOpen] = useState(false)
  const state = useDropState(path)
  const known = blockOf(ctx.blocks, node) !== undefined
  // Drop highlights take over the colour while something is dragged over the block.
  const tint =
    state === 'over' || state === 'fits' ? undefined : tintOf(blockOf(ctx.blocks, node)?.category)
  const label = node.ops.join(' OR ')
  const options = Object.entries(node.options ?? {})
  const count = node.args.length + options.length
  // Only leaves inside: the whole call reads on one line, like `ts_zscore(field, lookback)`.
  const inline = node.args.every((arg) => arg?.kind !== 'op')
  const close = `)${suffix}`

  // A comma follows each argument; the closing bracket ends the line when inline, or sits on its own line under the name.
  const tail = (position: number) => (position < count - 1 ? ',' : inline ? close : undefined)
  const arg = (index: number) => (
    <SlotView
      key={index}
      slot={node.args[index] ?? null}
      path={[...path, index]}
      suffix={tail(index)}
    />
  )
  const optionArgs = options.map(([key, value], index) => {
    const text = tail(node.args.length + index)
    return (
      <span key={`option:${key}`} className="flex items-center">
        <span className="num text-body-compact text-ink-subtle">
          {key}={String(value)}
        </span>
        {text && <Punct text={text} />}
      </span>
    )
  })
  const lines = argumentRows(node.args)

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: dragging is a pointer shortcut; the block menu makes the same edits by keyboard
    <div
      data-drop={pathKey(path)}
      draggable
      onDragStart={(event) => ctx.begin(event, { from: 'canvas', path, label })}
      className={cn(
        'flex w-fit max-w-full min-w-0 cursor-grab flex-col gap-1 rounded-lg border bg-surface-2 px-1.5 py-1.5 transition-colors select-none',
        state === 'over'
          ? 'border-primary bg-surface-3'
          : state === 'fits'
            ? 'border-primary'
            : !known
              ? 'border-pnl-negative-dim'
              : tint
                ? [tint.fill, tint.edge]
                : 'border-hairline hover:border-hairline-strong',
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-1">
        <span className="flex min-w-0 items-center">
          <Floating
            open={open}
            onOpenChange={setOpen}
            trigger={
              <Popover.Trigger className="num min-w-0 truncate rounded-sm py-0.5 pr-0.5 pl-1 text-left text-body text-ink hover:bg-surface-3">
                {label}
              </Popover.Trigger>
            }
          >
            <BlockMenu node={node} path={path} close={() => setOpen(false)} />
          </Floating>
          <Punct text={count === 0 ? `(${close}` : '('} />
        </span>
        {inline && node.args.map((_, index) => arg(index))}
        {inline && optionArgs}
      </div>
      {!inline && (
        <>
          <div
            className={cn(
              'ml-1 flex flex-col items-start gap-1 border-l pl-3',
              tint ? tint.edge : 'border-hairline-strong',
            )}
          >
            {lines.map((row, index) => (
              <div key={row[0]} className="flex max-w-full flex-wrap items-center gap-1.5">
                {row.map(arg)}
                {index === lines.length - 1 && optionArgs}
              </div>
            ))}
          </div>
          <Punct text={close} className="pl-1" />
        </>
      )}
    </div>
  )
}

function Punct({ text, className }: { text: string; className?: string }) {
  return <span className={cn('num text-body text-ink-subtle select-none', className)}>{text}</span>
}

/** Argument indexes grouped so neighbouring leaves and empty inputs share a row; every operator gets its own. */
function Leaf({ node, path }: { node: Exclude<TemplateNode, OperatorNode>; path: Path }) {
  const ctx = useBuilder()
  const state = useDropState(path)
  const twin = node.kind === 'var' && ctx.hoverTag === `${node.name}#${node.tag}`
  const label = node.kind === 'num' ? String(node.value) : node.name

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: dragging is a pointer shortcut; the block menu makes the same edits by keyboard
    <div
      data-drop={pathKey(path)}
      draggable
      onDragStart={(event) => ctx.begin(event, { from: 'canvas', path, label })}
      className={cn(
        'group relative inline-flex h-6 w-fit cursor-grab items-center gap-1 rounded-xs border bg-surface-3 px-2 transition-colors select-none hover:bg-surface-4 has-[input:focus-visible]:outline-2 has-[input:focus-visible]:outline-offset-2 has-[input:focus-visible]:outline-primary',
        state === 'over'
          ? 'border-primary bg-surface-3'
          : state === 'fits' || twin
            ? 'border-primary'
            : 'border-hairline-strong',
      )}
    >
      {node.kind === 'num' ? (
        <input
          key={node.value}
          type="number"
          aria-label="Number"
          defaultValue={node.value}
          onBlur={(event) => {
            const value = Number(event.target.value)
            if (event.target.value !== '' && Number.isFinite(value) && value !== node.value)
              ctx.change(put(ctx.root, path, { kind: 'num', value }))
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') event.currentTarget.blur()
          }}
          className="num field-sizing-content min-w-6 [appearance:textfield] bg-transparent text-body-compact text-ink outline-none [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
        />
      ) : (
        <span
          className={cn(
            'num text-body-compact',
            node.kind === 'var' ? 'text-ink' : 'text-ink-muted',
          )}
        >
          {label}
        </span>
      )}
      {node.kind === 'var' && <TagChip node={node} path={path} />}
      <button
        type="button"
        aria-label={`Remove ${label}`}
        className="absolute -top-2.5 -right-2.5 inline-flex size-6 items-center justify-center text-ink-subtle opacity-0 transition-opacity group-hover:opacity-100 hover:text-ink focus-visible:opacity-100 pointer-coarse:opacity-100 hover:[&>span]:bg-surface-3"
        onClick={() => ctx.change(put(ctx.root, path, null))}
      >
        {/* A 16px dot inside a 24px target (WCAG 2.5.8). */}
        <span className="inline-flex size-4 items-center justify-center rounded-xs border border-hairline-strong bg-surface-4 transition-colors">
          <XIcon className="size-2.5" />
        </span>
      </button>
    </div>
  )
}

/** The letter on a variable: same letter, same value. Hovering lights up its twins; clicking relinks it. */
function TagChip({ node, path }: { node: VariableNode; path: Path }) {
  const ctx = useBuilder()
  const [open, setOpen] = useState(false)
  const tags = tagsOf(ctx.root, node.name)
  const free = nextTag(ctx.root, node.name)
  const values = node.name === 'FIELD' ? null : ctx.options?.variables[node.name]
  const relink = (tag: string) => {
    ctx.change(retag(ctx.root, path, tag))
    setOpen(false)
  }

  return (
    <Floating
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Popover.Trigger
          aria-label={`${node.name} ${node.tag}`}
          onMouseEnter={() => ctx.setHoverTag(`${node.name}#${node.tag}`)}
          onMouseLeave={() => ctx.setHoverTag(null)}
          className="num inline-flex size-5 items-center justify-center rounded-xs bg-surface-3 text-caption text-ink-muted transition-colors hover:bg-surface-4 hover:text-ink"
        >
          {node.tag}
        </Popover.Trigger>
      }
    >
      <div className="flex w-56 max-w-full flex-col gap-0.5 p-1">
        {tags.map((tag) => (
          <button
            key={tag}
            type="button"
            onClick={() => relink(tag)}
            className={cn(
              'flex h-8 items-center justify-between gap-2 rounded-sm px-2 text-body transition-colors hover:bg-surface-4 hover:text-ink',
              tag === node.tag ? 'text-ink' : 'text-ink-muted',
            )}
          >
            <span>Same as {tag}</span>
            {tag === node.tag && (
              <CheckIcon className="size-3.5 shrink-0 text-primary" aria-hidden />
            )}
          </button>
        ))}
        {free && (
          <button
            type="button"
            onClick={() => relink(free)}
            className="flex h-8 items-center rounded-sm px-2 text-body text-ink-muted transition-colors hover:bg-surface-4 hover:text-ink"
          >
            {node.name === 'FIELD' ? 'Another Field' : 'Own Value'} ({free})
          </button>
        )}
        {values && (
          <p className="border-t border-hairline px-2 pt-1.5 pb-1 num text-body-compact text-ink-subtle">
            {values.join(' · ')}
          </p>
        )}
      </div>
    </Floating>
  )
}

function BlockMenu({ node, path, close }: { node: OperatorNode; path: Path; close: () => void }) {
  const ctx = useBuilder()
  const first = blockOf(ctx.blocks, node)
  const allowed = choices(node, ctx.blocks)
  const update = (next: OperatorNode) => ctx.change(put(ctx.root, path, next))

  const setOption = (key: string, value: number | boolean | string | undefined) => {
    const options = Object.fromEntries(
      Object.entries(node.options ?? {}).filter(([name]) => name !== key),
    )
    if (value !== undefined) options[key] = value
    // Every operator in a block must take the options it sets.
    const ops =
      value === undefined
        ? node.ops
        : node.ops.filter((name) => key in (ctx.blocks[name]?.options ?? {}))
    const next: OperatorNode = {
      kind: 'op',
      ops: ops.length > 0 ? ops : node.ops,
      args: node.args,
    }
    if (Object.keys(options).length > 0) next.options = options
    update(next)
  }

  return (
    <div className="flex w-80 max-w-full flex-col gap-3 p-3">
      {allowed.length > 1 && (
        <Fieldset
          legend="Operators"
          hint="The lab tries each and keeps what gives the best Sharpe."
        >
          <Chips
            label="Operators"
            items={allowed.map((block) => ({
              value: block.name,
              label: block.name,
              title: block.description ?? undefined,
            }))}
            value={node.ops}
            onChange={(ops) => {
              if (ops.length > 0) update({ ...node, ops })
            }}
          />
        </Fieldset>
      )}
      {first && Object.keys(first.options).length > 0 && (
        <Fieldset legend="Options">
          {Object.entries(first.options).map(([key, fallback]) => (
            <OptionInput
              key={key}
              name={key}
              fallback={fallback}
              value={node.options?.[key]}
              onChange={(value) => setOption(key, value)}
            />
          ))}
        </Fieldset>
      )}
      <div className="flex flex-wrap gap-2 border-t border-hairline pt-3">
        <Button
          size="sm"
          onClick={() => {
            ctx.change(unwrap(ctx.root, path, ctx.blocks))
            close()
          }}
        >
          Remove Block (Keep Input)
        </Button>
        <Button
          size="sm"
          variant="danger"
          onClick={() => {
            ctx.change(put(ctx.root, path, null))
            close()
          }}
        >
          Remove
        </Button>
      </div>
    </div>
  )
}

/** A text option, drafted locally and committed when it is valid.
 *
 * The draft is the point. Validating each keystroke against the finished shape and clearing
 * the value when it fails makes a list of numbers impossible to type: `0` is not a list, so
 * the first keystroke would be wiped before the comma ever arrived.
 */
function WordOption({
  name,
  fallback,
  value,
  onChange,
}: {
  name: string
  fallback: string
  value: number | boolean | string | undefined
  onChange: (value: number | boolean | string | undefined) => void
}) {
  const committed = typeof value === 'string' ? value : ''
  const [draft, setDraft] = useState(committed)
  // Follows the block when it changes underneath — another option edited, or an undo.
  const [seen, setSeen] = useState(committed)
  if (seen !== committed) {
    setSeen(committed)
    setDraft(committed)
  }

  // The same two shapes the backend accepts, so a refusal never arrives on save.
  const valid = (text: string) =>
    /^[A-Za-z][A-Za-z0-9_]*$/.test(text) ||
    /^-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?)+$/.test(text)

  const commit = (text: string) => {
    const clean = text.trim()
    if (clean === '') onChange(undefined)
    else if (valid(clean)) onChange(clean)
  }

  // Only after they have left the field: a list of numbers is invalid for most of the time
  // it takes to type one, and flagging that on every keystroke is noise, not help.
  const [left, setLeft] = useState(false)
  const clean = draft.trim()
  const bad = left && clean !== '' && !valid(clean)
  return (
    <label className="flex items-center justify-between gap-3 text-body text-ink-muted">
      <span className="num">{name}</span>
      <Input
        className="num w-40"
        spellCheck={false}
        autoComplete="off"
        placeholder={fallback}
        value={draft}
        aria-invalid={bad}
        // Drafted here, committed on the way out: committing mid-keystroke rewrote the
        // tree on every valid prefix of a list.
        onChange={(e) => {
          setLeft(false)
          setDraft(e.target.value)
        }}
        onBlur={() => {
          setLeft(true)
          if (clean === '' || valid(clean)) commit(draft)
          // Half-typed and left behind: back to what the block already held.
          else setDraft(committed)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') e.currentTarget.blur()
          if (e.key === 'Escape') setDraft(committed)
        }}
      />
    </label>
  )
}

function OptionInput({
  name,
  fallback,
  value,
  onChange,
}: {
  name: string
  fallback: number | boolean | string
  value: number | boolean | string | undefined
  onChange: (value: number | boolean | string | undefined) => void
}) {
  // An option whose default is text is either a name that chooses behaviour — `quantile(x,
  // driver = gaussian)` also takes `cauchy` and `uniform` — or a list of numbers, as in
  // `bucket(x, range = "0, 1, 0.1")`. Typed rather than picked from a list: BRAIN publishes
  // the default in the operator's signature but never the alternatives, so a list here
  // would be a guess that goes stale.
  if (typeof fallback === 'string') {
    return <WordOption name={name} fallback={fallback} value={value} onChange={onChange} />
  }
  if (typeof fallback === 'boolean') {
    return (
      <Checkbox
        label={<span className="num">{name}</span>}
        checked={Boolean(value ?? fallback)}
        onChange={(next) => onChange(next === fallback ? undefined : next)}
      />
    )
  }
  return (
    <label className="flex items-center justify-between gap-3 text-body text-ink-muted">
      <span className="num">{name}</span>
      <Input
        type="number"
        className="w-24"
        placeholder={String(fallback)}
        value={typeof value === 'number' ? value : ''}
        onChange={(e) => {
          const number = Number(e.target.value)
          onChange(e.target.value === '' || !Number.isFinite(number) ? undefined : number)
        }}
      />
    </label>
  )
}

/** Every block that fits an empty input, searchable, for placing a block without dragging. */
function BlockSearch({ socket, onPick }: { socket: Socket; onPick: (node: TemplateNode) => void }) {
  const ctx = useBuilder()
  const sections = paletteSections(ctx.root, ctx.blocks, ctx.options)
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => accepts(socket, item.node, ctx.blocks)),
    }))
    .filter((section) => section.items.length > 0)

  return (
    <Command
      loop
      className="flex w-72 max-w-full flex-col [&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1"
    >
      <div className="flex items-center gap-2 border-b border-hairline px-3 transition-colors focus-within:border-ink-subtle">
        <SearchIcon className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
        <Command.Input
          autoFocus
          placeholder="Search blocks…"
          className="h-9 flex-1 bg-transparent text-body text-ink outline-none placeholder:text-ink-subtle"
        />
      </div>
      <Command.List className="max-h-72 overflow-y-auto p-1">
        <Command.Empty className="px-3 py-4 text-center text-body text-ink-subtle">
          No block fits here.
        </Command.Empty>
        {sections.map((section) => (
          <Command.Group key={section.heading} heading={section.heading}>
            {section.items.map((item) => (
              <Command.Item
                key={item.key}
                value={item.key}
                keywords={[item.label]}
                onSelect={() => onPick(item.node)}
                className="flex h-8 cursor-default items-center rounded-sm px-2 text-body text-ink-muted select-none data-[selected=true]:bg-surface-4 data-[selected=true]:text-ink"
              >
                <span className="num truncate">{item.label}</span>
              </Command.Item>
            ))}
          </Command.Group>
        ))}
      </Command.List>
    </Command>
  )
}
