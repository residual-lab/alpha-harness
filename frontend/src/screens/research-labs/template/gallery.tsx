/**
 * The Templates panel: built-in templates, shown as their expressions rather than names, and the
 * user's saved ones — each with its operators and fields, and the kinds of Alpha it makes. A
 * built-in one opens a popup with the template and every variation of it.
 */

import { Dialog as BDialog } from '@base-ui/react/dialog'
import { ChevronDownIcon, ChevronRightIcon, ChevronUpIcon, FilePlusIcon, XIcon } from 'lucide-react'
import { type ReactNode, useRef, useState } from 'react'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { Badge, Button, ErrorNotice, Panel, Skeleton } from '@/ui/kit'
import { BACKDROP } from '@/ui/overlay'
import type { TemplateStats, TemplateSummary, VariableDef } from './api'
import { namesIn } from './variables'

/** A built-in template: an idea, and complete templates built on it. */
export interface Family {
  /** The idea as the gallery shows it: its lookback written d, its signals `...`. */
  shown: string
  /** What opening the idea writes into the editor; each `...` is a signal to fill. */
  text: string
  /** Complete templates built on it, as typed. */
  variations: string[]
}

export const GALLERY: Family[] = [
  {
    shown: 'if_else(abs(ts_arg_min(low, d) - ts_arg_max(high, d)) > d / 2, ..., ...)',
    text: 'if_else(abs(ts_arg_min(low, $lookback) - ts_arg_max(high, $lookback)) > $lookback / 2, ..., ...)',
    variations: [
      'r = rank($field);\nif_else(abs(ts_arg_min(low, $lookback) - ts_arg_max(high, $lookback)) > $lookback / 2, r, 1 - r)',
      'rank(if_else(abs(ts_arg_min(low, $lookback) - ts_arg_max(high, $lookback)) > $lookback / 2, $field, -$field))',
    ],
  },
]

/** Where each card's stats sit in one batch: built-ins by family and variation, then saved. */
export const galleryKey = (family: number, variation: number | null) =>
  `gallery:${family}:${variation ?? 'idea'}`
export const savedKey = (id: number) => `saved:${id}`

/** A card's template as it opens: what each variable starts as, and what that makes it. */
export interface Card {
  variables: Record<string, VariableDef>
  stats: TemplateStats | undefined
}

/** A count, or its range where the variables' values differ: `8`, `7–9`. */
const span = ([fewest = 0, most = 0]: number[]) =>
  fewest === most ? fmt.int(most) : `${fmt.int(fewest)}–${fmt.int(most)}`

/**
 * One Power Pool budget: what a template uses out of what Power Pool allows, a pip for each.
 * Pips its Alphas always use are solid, ones only some use are faint, and all turn amber once
 * any Alpha is over.
 */
function Budget({ label, used, limit }: { label: string; used: number[]; limit: number }) {
  const [fewest = 0, most = 0] = used
  const over = most > limit
  const noun = label.toLowerCase()
  return (
    <span
      className="inline-flex items-center gap-1.5"
      title={
        over
          ? `${span(used)} ${noun}: more than the ${limit} Power Pool allows`
          : `${span(used)} of the ${limit} ${noun} Power Pool allows`
      }
    >
      <span className="text-caption text-ink-subtle">{label}</span>
      <span className={cn('num text-body-compact', over ? 'text-status-warning' : 'text-ink')}>
        {span(used)}
        <span className="text-ink-subtle">/{limit}</span>
      </span>
      <span aria-hidden className="flex items-center gap-0.5">
        {Array.from({ length: limit }, (_, i) => (
          <span
            key={i}
            className={cn(
              'h-3 w-1 rounded-pill',
              over
                ? 'bg-status-warning'
                : i < fewest
                  ? 'bg-primary'
                  : i < most
                    ? 'bg-primary/40'
                    : 'bg-surface-4',
            )}
          />
        ))}
      </span>
    </span>
  )
}

/** Operators and fields against Power Pool's limits, and the kinds of Alpha a template makes. */
export function Facts({
  stats,
  className,
}: {
  stats: TemplateStats | null | undefined
  className?: string
}) {
  if (!stats || stats.problem) return null
  const [operators = 0, fields = 0] = stats.limits
  return (
    <span className={cn('flex flex-wrap items-center gap-x-4 gap-y-1.5', className)}>
      <Budget label="Operators" used={stats.operators} limit={operators} />
      <Budget label="Fields" used={stats.fields} limit={fields} />
      {stats.powerPool && stats.holes === 0 && (
        <Badge
          tone="outline"
          title="Every Alpha it makes is within Power Pool's operators and fields; ts_backfill and grouping fields are not counted"
        >
          Power Pool
        </Badge>
      )}
      {stats.singleDataset && (
        <Badge tone="outline" title="Every Alpha it makes reads one dataset, grouping fields aside">
          Single Dataset
        </Badge>
      )}
      {stats.holes > 0 && (
        <span
          className="text-body-compact text-ink-subtle"
          title="Each ... is a signal to write before it can run"
        >
          {fmt.int(stats.holes)} Signal{stats.holes === 1 ? '' : 's'} to Write
        </span>
      )}
      {stats.missing.length > 0 && (
        <span className="text-body-compact text-status-warning">
          Needs {stats.missing.join(', ')}
        </span>
      )}
    </span>
  )
}

/** An expression set as code: whole, wrapped where it must, each line its own. */
function Expression({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <code className={cn('num text-body-compact break-all whitespace-pre-wrap text-ink', className)}>
      {children}
    </code>
  )
}

const CARD =
  'flex w-full flex-col items-start gap-1.5 rounded-md border px-3 py-2 text-left transition-colors'
const IDLE = 'border-hairline bg-surface-1 hover:border-hairline-strong hover:bg-surface-2'

export function TemplatesPanel({
  saved,
  savedState,
  selected,
  dirty,
  cards,
  onOpenText,
  onOpenSaved,
  onNew,
  writing,
}: {
  saved: TemplateSummary[]
  savedState: { pending: boolean; error: unknown }
  selected: number | null
  dirty: boolean
  /** By {@link galleryKey} and {@link savedKey}. */
  cards: Map<string, Card>
  onOpenText: (text: string) => void
  onOpenSaved: (template: TemplateSummary) => void
  onNew: () => void
  /** A template is open in the editor: the gallery starts folded, out of its way. */
  writing: boolean
}) {
  const [family, setFamily] = useState<number | null>(null)
  const [open, setOpen] = useState(!writing)
  // Opening one folds the gallery, so the editor it opened into is what follows.
  const folding =
    <A extends unknown[]>(then: (...args: A) => void) =>
    (...args: A) => {
      setOpen(false)
      then(...args)
    }
  return (
    <Panel
      title="Templates"
      actions={
        <>
          <Button size="sm" variant="ghost" onClick={folding(onNew)}>
            <FilePlusIcon />
            New Template
          </Button>
          <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? <ChevronUpIcon /> : <ChevronDownIcon />}
            {open ? 'Hide' : 'Show'}
          </Button>
        </>
      }
    >
      {open && (
        <div className="flex flex-col gap-4">
          <section className="flex flex-col gap-2">
            <h3 className="text-caption font-medium text-ink-subtle">Built-in</h3>
            <div className="grid gap-2 lg:grid-cols-2">
              {GALLERY.map((f, index) => (
                <button
                  key={f.text}
                  type="button"
                  className={cn(CARD, IDLE)}
                  onClick={() => setFamily(index)}
                >
                  <Expression>{f.shown}</Expression>
                  <span className="flex w-full flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
                    <Facts stats={cards.get(galleryKey(index, null))?.stats} />
                    {f.variations.length > 0 && (
                      <span className="inline-flex items-center gap-0.5 text-body-compact text-ink-subtle">
                        {fmt.int(f.variations.length)} Variation
                        {f.variations.length === 1 ? '' : 's'}
                        <ChevronRightIcon className="size-3.5" aria-hidden />
                      </span>
                    )}
                  </span>
                </button>
              ))}
            </div>
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-caption font-medium text-ink-subtle">Saved</h3>
            {savedState.error ? (
              <ErrorNotice error={savedState.error} title="Could not load templates" />
            ) : savedState.pending ? (
              <Skeleton className="h-20" />
            ) : saved.length === 0 ? (
              <p className="text-body-compact text-ink-subtle">
                Nothing saved yet. Save As keeps the template you are writing here.
              </p>
            ) : (
              <div className="-m-1 grid max-h-96 gap-2 overflow-y-auto p-1 sm:grid-cols-2 xl:grid-cols-3">
                {saved.map((template) => {
                  const open = template.id === selected
                  return (
                    <button
                      key={template.id}
                      type="button"
                      aria-pressed={open}
                      onClick={() => folding(onOpenSaved)(template)}
                      className={cn(CARD, open ? 'border-hairline-strong bg-surface-2' : IDLE)}
                    >
                      <span className="flex w-full items-center justify-between gap-2">
                        <span className="text-title min-w-0 truncate" title={template.name}>
                          {template.name}
                        </span>
                        {open && dirty && (
                          <span className="shrink-0 text-body-compact text-ink-subtle">Edited</span>
                        )}
                      </span>
                      {template.description && (
                        <span className="line-clamp-2 text-body-compact text-ink-subtle">
                          {template.description}
                        </span>
                      )}
                      <span className="line-clamp-3">
                        <Expression>{template.text}</Expression>
                      </span>
                      <Facts stats={cards.get(savedKey(template.id))?.stats} />
                    </button>
                  )
                })}
              </div>
            )}
          </section>
        </div>
      )}
      {family !== null && GALLERY[family] && (
        <FamilyDialog
          family={GALLERY[family]}
          index={family}
          cards={cards}
          onClose={() => setFamily(null)}
          onOpen={(text) => {
            setFamily(null)
            folding(onOpenText)(text)
          }}
        />
      )}
    </Panel>
  )
}

/**
 * A built-in template and every variation of it, laid out like Settings: the list down the left,
 * the one chosen on the right, where it opens into the editor.
 */
function FamilyDialog({
  family,
  index,
  cards,
  onClose,
  onOpen,
}: {
  family: Family
  index: number
  cards: Map<string, Card>
  onClose: () => void
  onOpen: (text: string) => void
}) {
  const entries = [
    { key: galleryKey(index, null), label: 'Template', text: family.text },
    ...family.variations.map((text, v) => ({
      key: galleryKey(index, v),
      label: `Variation ${v + 1}`,
      text,
    })),
  ]
  const [chosen, setChosen] = useState(0)
  const entry = entries[chosen] ?? entries[0]
  const card = entry ? cards.get(entry.key) : undefined
  const content = useRef<HTMLDivElement>(null)

  return (
    <BDialog.Root open onOpenChange={(next) => !next && onClose()}>
      <BDialog.Portal>
        <BDialog.Backdrop className={BACKDROP} />
        <BDialog.Popup
          initialFocus={content}
          className="fixed top-1/2 left-1/2 z-50 flex h-[min(40rem,calc(100vh-4rem))] w-[calc(100%-2rem)] max-w-4xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-lg border border-hairline-strong bg-surface-2 shadow-float outline-none sm:flex-row"
        >
          <nav
            aria-label="The template and its variations"
            className="flex shrink-0 flex-col gap-0.5 overflow-y-auto border-b border-hairline p-2 sm:w-56 sm:border-r sm:border-b-0"
          >
            <BDialog.Title className="px-2 pt-1 pb-2 text-caption font-medium text-ink-subtle">
              Built-in Template
            </BDialog.Title>
            {entries.map((e, i) => {
              const stats = cards.get(e.key)?.stats
              return (
                <button
                  key={e.key}
                  type="button"
                  aria-current={i === chosen ? 'true' : undefined}
                  onClick={() => setChosen(i)}
                  className="flex flex-col items-start gap-0.5 rounded-md px-2 py-1.5 text-left text-body text-ink-muted transition-colors hover:bg-surface-3 hover:text-ink aria-[current=true]:bg-surface-4 aria-[current=true]:text-ink"
                >
                  <span className={cn(i === chosen && 'font-medium')}>{e.label}</span>
                  {stats && !stats.problem && (
                    <span className="num text-caption text-ink-subtle">
                      {span(stats.operators)}/{stats.limits[0]} operators · {span(stats.fields)}/
                      {stats.limits[1]} fields
                    </span>
                  )}
                </button>
              )
            })}
          </nav>
          <div
            ref={content}
            tabIndex={-1}
            className="relative flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-5 outline-none"
          >
            <BDialog.Close
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Close"
                  className="absolute top-3 right-3"
                />
              }
            >
              <XIcon />
            </BDialog.Close>
            {entry && (
              <>
                <div className="flex flex-col gap-1 pr-10">
                  <h2 className="text-title font-medium text-ink">{entry.label}</h2>
                  <p className="text-body-compact text-ink-subtle">
                    {chosen === 0
                      ? 'The idea itself. Each ... is a signal for you to write.'
                      : 'A complete template built on it, ready to search.'}
                  </p>
                </div>
                <Expression className="rounded-md border border-hairline bg-surface-1 p-3 text-body">
                  {entry.text}
                </Expression>
                <Facts stats={card?.stats} />
                <Variables text={entry.text} variables={card?.variables ?? {}} />
                <div className="mt-auto flex justify-end pt-2">
                  <Button variant="primary" onClick={() => onOpen(entry.text)}>
                    Open in Editor
                  </Button>
                </div>
              </>
            )}
          </div>
        </BDialog.Popup>
      </BDialog.Portal>
    </BDialog.Root>
  )
}

/** What each `$name` in a template starts as when it opens. */
function Variables({ text, variables }: { text: string; variables: Record<string, VariableDef> }) {
  const names = namesIn(text)
  if (names.length === 0) return null
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-caption font-medium text-ink-subtle">Variables</h3>
      <ul className="flex flex-col gap-1.5">
        {names.map((name) => {
          const def = variables[name]
          return (
            <li key={name} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
              <code className="num text-body-compact font-medium text-ink">${name}</code>
              <span className="min-w-0 text-body-compact break-words text-ink-subtle">
                {!def ? (
                  'Not defined yet: you choose its fields, or type its values, once it is open.'
                ) : def.kind === 'values' ? (
                  <>
                    Values <code className="num text-ink-muted">{def.values}</code>
                  </>
                ) : (
                  `Fields from ${listed(def.dataset_ids)}`
                )}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** `a, b and 4 more`: enough to recognise a choice without listing a whole category. */
function listed(ids: string[]): string {
  const shown = ids.slice(0, 3).join(', ')
  return ids.length > 3 ? `${shown} and ${fmt.int(ids.length - 3)} more` : shown
}
