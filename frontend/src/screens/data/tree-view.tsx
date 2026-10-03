/** The Fields tab's dataset filter: a Category → Subcategory → Dataset tree. */

import { ChevronRightIcon, CopyIcon, DownloadIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { type CatalogFacets, catalog } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { saveText } from '@/lib/download'
import { fmt } from '@/lib/format'
import { Button, Input } from '@/ui/kit'
import { ContextMenu } from '@/ui/overlay'
import { DatasetChips } from './dataset-chips'
import { buildTree } from './dataset-tree'

/**
 * The dataset filter as a Category → Subcategory → Dataset tree. The choice is always the
 * dataset ids under what is ticked, which is what the fields query and the labs take.
 */
export function DatasetTree({
  scope,
  source,
  counts,
  names,
  value,
  onChange,
  title = 'Datasets',
  searchLabel = 'Search Categories, Subcategories and Datasets',
}: {
  /** The market, for copying a node's fields. */
  scope: Scope
  /** The market's whole tree, unfiltered, so a ticked category takes every dataset in it. */
  source: CatalogFacets
  counts: CatalogFacets | undefined
  names: Map<string, string>
  value: string[]
  onChange: (ids: string[]) => void
  /** Names the tree, and tells two on one screen apart. */
  title?: string
  searchLabel?: string
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set())
  const tree = useMemo(() => buildTree(source), [source])
  const chosen = new Set(value)
  const term = query.trim().toLowerCase()
  const nameOf = (id: string) => names.get(id) ?? id
  const matches = (text: string) => text.toLowerCase().includes(term)
  const hit = (id: string) => matches(id) || matches(nameOf(id))
  // Every field of a category, subcategory or dataset, as Copy Selected Data Fields writes it:
  // to the clipboard, or to a Markdown file named by the node's BRAIN id.
  const actions = (ids: string[], where: string, file: string) => ({
    onCopy: () =>
      catalog
        .outline(scope, { dataset_ids: ids })
        .then(async (outline) => {
          await navigator.clipboard.writeText(outline.text)
          toast.success(`Copied ${fmt.int(outline.fields)} Data Fields`, { description: where })
        })
        .catch((e: unknown) =>
          toast.error('Could not copy the Data Fields', { description: errorMessage(e) }),
        ),
    onDownload: () =>
      catalog
        .outline(scope, { dataset_ids: ids })
        .then((outline) => {
          saveText(`${file}.md`, `${outline.text}\n`)
          toast.success(`Downloaded ${file}.md`, {
            description: `${fmt.int(outline.fields)} Data Fields from ${where}`,
          })
        })
        .catch((e: unknown) =>
          toast.error('Could not download the Data Fields', { description: errorMessage(e) }),
        ),
  })

  const fieldsIn = counts && new Map(counts.datasets.map((d) => [d.id, d.n]))
  const total = (ids: string[]) =>
    fieldsIn ? ids.reduce((sum, id) => sum + (fieldsIn.get(id) ?? 0), 0) : null

  const toggle = (ids: string[], on: boolean) => {
    const next = new Set(value)
    for (const id of ids) {
      if (on) next.add(id)
      else next.delete(id)
    }
    onChange([...next])
  }
  const expand = (key: string) =>
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  // A search opens everything it shows.
  const isOpen = (key: string) => term !== '' || open.has(key)

  // Most fields first at every level, by the counts shown beside each row: those follow the
  // other filters, so the order does too. Ties, and the moment before counts arrive, go by name.
  const byCount = <T,>(items: T[], ids: (item: T) => string[], name: (item: T) => string) =>
    [...items].sort(
      (a, b) => (total(ids(b)) ?? 0) - (total(ids(a)) ?? 0) || name(a).localeCompare(name(b)),
    )
  const byDataset = (ids: string[]) =>
    byCount(
      ids,
      (id) => [id],
      (id) => nameOf(id),
    )

  // A matching category or subcategory shows all of itself; otherwise only the datasets that match.
  const shown = byCount(
    tree.flatMap((trunk) => {
      const whole = !term || matches(trunk.name)
      const subs = byCount(
        trunk.subcategories.flatMap((branch) => {
          const ids = whole || matches(branch.name) ? branch.ids : branch.ids.filter(hit)
          return ids.length > 0 ? [{ branch, ids: byDataset(ids) }] : []
        }),
        (s) => s.branch.ids,
        (s) => s.branch.name,
      )
      const loose = byDataset(whole ? trunk.loose : trunk.loose.filter(hit))
      return whole || subs.length > 0 || loose.length > 0 ? [{ trunk, subs, loose }] : []
    }),
    (t) => t.trunk.ids,
    (t) => t.trunk.name,
  )

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex min-h-7 flex-wrap items-center justify-between gap-2">
        <h3 className="text-caption font-medium text-ink-muted">
          {title}
          {value.length > 0 && (
            <span className="num text-ink-subtle"> · {fmt.int(value.length)} selected</span>
          )}
        </h3>
        {value.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => onChange([])}>
            Clear
          </Button>
        )}
      </div>
      <DatasetChips
        tree={tree}
        value={value}
        nameOf={nameOf}
        onRemove={(ids) => toggle(ids, false)}
      />
      <Input
        placeholder={searchLabel}
        aria-label={searchLabel}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div
        role="group"
        aria-label={`${title} by category`}
        className="flex max-h-80 flex-col overflow-y-auto rounded-md border border-hairline py-1"
      >
        {tree.length === 0 ? (
          <p className="px-3 py-2 text-body-compact text-pretty text-ink-subtle">
            No datasets in this market yet. Download it in BRAIN › Sync.
          </p>
        ) : shown.length === 0 ? (
          <p className="px-3 py-2 text-body-compact text-ink-subtle">No match.</p>
        ) : (
          shown.map(({ trunk, subs, loose }) => (
            <div key={trunk.key} className="flex flex-col">
              <TreeRow
                depth={0}
                label={trunk.name}
                ids={trunk.ids}
                chosen={chosen}
                count={total(trunk.ids)}
                open={isOpen(trunk.key)}
                onExpand={() => expand(trunk.key)}
                onToggle={(on) => toggle(trunk.ids, on)}
                {...actions(trunk.ids, trunk.name, trunk.id || 'uncategorised')}
              />
              {isOpen(trunk.key) && (
                <>
                  {subs.map(({ branch, ids }) => (
                    <div key={branch.key} className="flex flex-col">
                      <TreeRow
                        depth={1}
                        label={branch.name}
                        ids={branch.ids}
                        chosen={chosen}
                        count={total(branch.ids)}
                        open={isOpen(branch.key)}
                        onExpand={() => expand(branch.key)}
                        onToggle={(on) => toggle(branch.ids, on)}
                        {...actions(branch.ids, `${trunk.name} > ${branch.name}`, branch.id)}
                      />
                      {isOpen(branch.key) &&
                        ids.map((id) => (
                          <TreeRow
                            key={id}
                            depth={2}
                            label={nameOf(id)}
                            title={id}
                            ids={[id]}
                            chosen={chosen}
                            count={total([id])}
                            onToggle={(on) => toggle([id], on)}
                            {...actions([id], `${trunk.name} > ${branch.name} > ${nameOf(id)}`, id)}
                          />
                        ))}
                    </div>
                  ))}
                  {loose.map((id) => (
                    <TreeRow
                      key={id}
                      depth={1}
                      label={nameOf(id)}
                      title={id}
                      ids={[id]}
                      chosen={chosen}
                      count={total([id])}
                      onToggle={(on) => toggle([id], on)}
                      {...actions([id], `${trunk.name} > ${nameOf(id)}`, id)}
                    />
                  ))}
                </>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}

/** One node: its checkbox is ticked when every dataset under it is, and part-ticked when some are. */
function TreeRow({
  depth,
  label,
  title,
  ids,
  chosen,
  count,
  open,
  onExpand,
  onToggle,
  onCopy,
  onDownload,
}: {
  depth: 0 | 1 | 2
  label: string
  title?: string
  ids: string[]
  chosen: Set<string>
  count: number | null
  open?: boolean
  onExpand?: () => void
  onToggle: (on: boolean) => void
  onCopy: () => void
  onDownload: () => void
}) {
  const on = ids.filter((id) => chosen.has(id)).length
  const all = on === ids.length
  const some = on > 0 && !all

  return (
    <ContextMenu
      items={[
        { label: 'Copy Data Fields', icon: <CopyIcon />, onClick: onCopy },
        { label: 'Download as Markdown', icon: <DownloadIcon />, onClick: onDownload },
      ]}
    >
      <div
        className={cn(
          'flex h-7 shrink-0 items-center gap-1 pr-3 text-body-compact transition-colors hover:bg-surface-2 data-[popup-open]:bg-surface-2',
          depth === 0 ? 'pl-1.5' : depth === 1 ? 'pl-6' : 'pl-10.5',
        )}
      >
        {onExpand ? (
          <button
            type="button"
            aria-label={`${open ? 'Collapse' : 'Expand'} ${label}`}
            aria-expanded={open}
            onClick={onExpand}
            className="shrink-0 rounded-xs p-0.5 text-ink-subtle transition-colors hover:text-ink"
          >
            <ChevronRightIcon
              className={cn('size-3.5 transition-transform', open && 'rotate-90')}
              aria-hidden
            />
          </button>
        ) : (
          <span className="w-4.5 shrink-0" aria-hidden />
        )}
        <label
          title={title ?? label}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-2"
        >
          <input
            type="checkbox"
            className="size-3.5 shrink-0"
            checked={all}
            ref={(el) => {
              if (el) el.indeterminate = some
            }}
            onChange={(e) => onToggle(e.target.checked)}
          />
          <span
            className={cn(
              'truncate',
              depth === 0 && 'font-medium',
              on > 0 ? 'text-ink' : 'text-ink-muted',
            )}
          >
            {label}
          </span>
        </label>
        {count != null && <span className="num shrink-0 text-ink-subtle">{fmt.int(count)}</span>}
      </div>
    </ContextMenu>
  )
}
