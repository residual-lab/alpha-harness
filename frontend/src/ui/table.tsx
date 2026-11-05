/**
 * A virtualised table: only visible rows mount, so a 500-row page scrolls without layout cost.
 * Sorting and paging are the backend's job (DuckDB); this only asks.
 */

import { useVirtualizer } from '@tanstack/react-virtual'
import { ArrowDownIcon, ArrowUpIcon, ChevronLeftIcon, ChevronRightIcon } from 'lucide-react'
import {
  Children,
  cloneElement,
  isValidElement,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from 'react'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { Button, Checkbox, Empty, ErrorNotice, Skeleton } from './kit'

export interface Column<T> {
  key: string
  header: ReactNode
  cell: (row: T) => ReactNode
  /** CSS grid track, e.g. `120px` or `minmax(240px,2fr)`. */
  width?: string
  align?: 'left' | 'right'
  sortable?: boolean
}

export interface Sort {
  key: string
  desc: boolean
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  activeKey,
  onRowClick,
  rowClass,
  sort,
  onSort,
  selected,
  onSelect,
  loading,
  error,
  empty = 'Nothing to show.',
  maxHeight = '70vh',
  rowHeight = 28,
  label,
  page,
}: {
  rows: T[]
  columns: Column<T>[]
  rowKey: (row: T) => string
  /** The row open beside the table, in a sheet or a pane: marked, and kept in view as it moves. */
  activeKey?: string | null | undefined
  /** The event comes too, so a table can treat a modified click as its own action, and the
   *  rows in the order shown, for stepping through them from what the click opened. */
  onRowClick?: (row: T, event: React.MouseEvent | React.KeyboardEvent, shown: T[]) => void
  /** Extra classes for one row, so a table can mark rows that mean something. */
  rowClass?: (row: T) => string | undefined
  sort?: Sort
  onSort?: (sort: Sort) => void
  /** With `onSelect`, adds a checkbox column. Shift-click ticks or unticks a run of rows. */
  selected?: ReadonlySet<string>
  onSelect?: (keys: string[], on: boolean) => void
  loading?: boolean
  /** A failed query: shown in place of the empty state, which would claim there is nothing. */
  error?: unknown
  empty?: ReactNode
  /** Explicitly `undefined` keeps the default, so a caller can size it only sometimes. */
  maxHeight?: string | undefined
  rowHeight?: number
  label: string
  /** The page shown, when the rows are paged: a new page, like a new sort, starts at its top. */
  page?: number
}) {
  const scroller = useRef<HTMLDivElement>(null)
  // The row last ticked, by key so a list refiltered since cannot aim a run at another row,
  // and whether shift is held for this one, for ticking a run.
  const lastTicked = useRef<string | null>(null)
  const shift = useRef(false)
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run to start each new sort or page at its top
  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 })
    lastTicked.current = null
  }, [sort?.key, sort?.desc, page])
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  })
  const selectable = selected !== undefined && onSelect !== undefined
  const activeIndex = activeKey == null ? -1 : rows.findIndex((row) => rowKey(row) === activeKey)
  // Stepping through rows in a sheet scrolls the table along, so closing it lands on the row.
  useEffect(() => {
    if (activeIndex >= 0) virtual.scrollToIndex(activeIndex, { align: 'auto' })
  }, [activeIndex, virtual])
  // The rows are one stop for Tab, moved between with the arrow keys: the last one focused,
  // else the open one, else the first.
  const [focused, setFocused] = useState<number | null>(null)
  const stop = focused !== null && focused < rows.length ? focused : Math.max(activeIndex, 0)
  const focusRow = (index: number) => {
    virtual.scrollToIndex(index, { align: 'auto' })
    requestAnimationFrame(() =>
      scroller.current?.querySelector<HTMLElement>(`[data-row-index="${index}"]`)?.focus(),
    )
  }
  const template = [
    selectable ? '36px' : null,
    ...columns.map((c) => c.width ?? 'minmax(96px,1fr)'),
  ]
    .filter(Boolean)
    .join(' ')

  const header = (
    <div role="rowgroup" className="sticky top-0 z-10">
      <div
        role="row"
        aria-rowindex={1}
        className="grid border-b border-hairline-strong bg-surface-1 select-none"
        style={{ gridTemplateColumns: template }}
      >
        {selectable && (
          <div role="columnheader" className="flex items-center justify-center">
            <Checkbox
              label={null}
              aria-label="Select every row shown"
              checked={rows.length > 0 && rows.every((row) => selected.has(rowKey(row)))}
              indeterminate={
                rows.some((row) => selected.has(rowKey(row))) &&
                !rows.every((row) => selected.has(rowKey(row)))
              }
              disabled={rows.length === 0}
              onChange={(on) => onSelect(rows.map(rowKey), on)}
            />
          </div>
        )}
        {columns.map((column) => {
          const active = sort?.key === column.key
          const Arrow = sort?.desc ? ArrowDownIcon : ArrowUpIcon
          const content = (
            // Headers wrap rather than clip: a long name would otherwise widen its track until
            // the whole table needed a horizontal scrollbar to show short values. The sort arrow
            // sits *inside* the text, so it wraps with the last word instead of being a rigid
            // sibling that collides with it in a narrow column.
            <span className={cn('min-w-0 text-balance', column.align === 'right' && 'text-right')}>
              {wholeWords(column.header)}
              {active && (
                <>
                  {/* A word joiner keeps the arrow on the line of the last word. */}
                  {'\u2060'}
                  <Arrow className="ml-1 inline size-3 align-text-bottom text-primary" />
                </>
              )}
            </span>
          )
          return (
            <div
              key={column.key}
              role="columnheader"
              aria-sort={active ? (sort.desc ? 'descending' : 'ascending') : undefined}
              className={cn(
                // min-w-0: a grid item defaults to min-width:auto, which lets a long header
                // push past its own track instead of wrapping inside it. Bottom-aligned so a
                // header that took two lines still sits on the row's baseline.
                'flex min-h-8 min-w-0 items-end px-3 pt-1.5 pb-1.5 text-caption font-medium uppercase leading-tight tracking-wide text-ink-subtle',
                column.align === 'right' && 'justify-end',
              )}
            >
              {column.sortable && onSort ? (
                <button
                  type="button"
                  className={cn(
                    'inline-flex min-w-0 items-end transition-colors hover:text-ink',
                    active && 'text-ink',
                  )}
                  onClick={() => onSort({ key: column.key, desc: active ? !sort.desc : true })}
                >
                  {content}
                </button>
              ) : (
                content
              )}
            </div>
          )
        })}
      </div>
    </div>
  )

  return (
    <div
      ref={scroller}
      role="table"
      aria-label={label}
      aria-rowcount={rows.length + 1}
      className="min-w-0 overflow-auto"
      style={{ maxHeight }}
    >
      {/*
        fit-content: fills the panel, and only scrolls when the columns' minimum widths do not fit.
        Presentational, so the rowgroups below stay owned by the table rather than by a plain div.
      */}
      <div role="presentation" className="w-fit min-w-full">
        {header}
        {loading && rows.length === 0 ? (
          <div className="flex flex-col gap-1 p-2">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-6" />
            ))}
          </div>
        ) : rows.length === 0 && error ? (
          <ErrorNotice error={error} className="m-2" />
        ) : rows.length === 0 ? (
          <Empty title={empty} />
        ) : (
          <div
            role="rowgroup"
            // Rows from before a new sort or page still showing while it loads: dimmed, not live.
            className={cn('relative transition-opacity', loading && 'opacity-60')}
            aria-busy={loading || undefined}
            style={{ height: virtual.getTotalSize() }}
          >
            {virtual.getVirtualItems().map((item) => {
              const row = rows[item.index]
              if (row === undefined) return null
              const key = rowKey(row)
              const isSelected = selectable && selected.has(key)
              const isActive = item.index === activeIndex
              return (
                <div
                  key={key}
                  role="row"
                  aria-rowindex={item.index + 2}
                  aria-selected={selectable ? isSelected : undefined}
                  aria-current={isActive || undefined}
                  data-row-index={item.index}
                  tabIndex={onRowClick ? (item.index === stop ? 0 : -1) : undefined}
                  onFocus={onRowClick ? () => setFocused(item.index) : undefined}
                  onClick={onRowClick ? (e) => onRowClick(row, e, rows) : undefined}
                  onKeyDown={
                    onRowClick
                      ? (e) => {
                          // A key meant for a button or menu inside the row is not the row's.
                          if (e.target !== e.currentTarget) return
                          if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                            e.preventDefault()
                            const step = e.key === 'ArrowDown' ? 1 : -1
                            focusRow(Math.max(0, Math.min(rows.length - 1, item.index + step)))
                            return
                          }
                          if (e.key !== 'Enter' && e.key !== ' ') return
                          e.preventDefault()
                          onRowClick(row, e, rows)
                        }
                      : undefined
                  }
                  className={cn(
                    'absolute inset-x-0 grid border-b border-hairline-subtle bg-surface-1 text-body-compact transition-colors',
                    // A rule down the row's left edge as well as the fill, which is a faint step to
                    // follow across a wide screen: hairline on hover, lavender on the open row.
                    onRowClick &&
                      'cursor-pointer hover:bg-surface-2 hover:shadow-[inset_2px_0_0_0_var(--color-hairline-strong)] focus-visible:bg-surface-2 focus-visible:-outline-offset-2',
                    isSelected && 'bg-surface-2',
                    // Held through hover, which otherwise repaints the row as if it were not open.
                    isActive &&
                      'bg-primary-subtle shadow-[inset_2px_0_0_0_var(--color-primary)] hover:bg-primary-subtle hover:shadow-[inset_2px_0_0_0_var(--color-primary)]',
                    rowClass?.(row),
                  )}
                  style={{
                    gridTemplateColumns: template,
                    height: rowHeight,
                    transform: `translateY(${item.start}px)`,
                  }}
                >
                  {selectable && (
                    <div
                      role="cell"
                      className="flex items-center justify-center"
                      onClickCapture={(e) => {
                        shift.current = e.shiftKey
                      }}
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    >
                      <Checkbox
                        label={null}
                        aria-label="Select row"
                        checked={isSelected}
                        onChange={(on) => {
                          const at = shift.current ? lastTicked.current : null
                          const from = at === null ? -1 : rows.findIndex((r) => rowKey(r) === at)
                          shift.current = false
                          lastTicked.current = key
                          const run =
                            from < 0
                              ? [row]
                              : rows.slice(
                                  Math.min(from, item.index),
                                  Math.max(from, item.index) + 1,
                                )
                          onSelect(run.map(rowKey), on)
                        }}
                      />
                    </div>
                  )}
                  {columns.map((column) => {
                    const content = column.cell(row)
                    return (
                      <div
                        key={column.key}
                        role="cell"
                        className={cn(
                          'flex min-w-0 items-center overflow-hidden px-3',
                          column.align === 'right' && 'mono-metric justify-end',
                        )}
                      >
                        {/* A flex box clips without an ellipsis; plain text gets a block that truncates. */}
                        {typeof content === 'string' || typeof content === 'number' ? (
                          <span
                            className="truncate"
                            title={content === DASH ? undefined : String(content)}
                          >
                            {content}
                          </span>
                        ) : (
                          content
                        )}
                      </div>
                    )
                  })}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export function Pager({
  total,
  offset,
  limit,
  onChange,
}: {
  total: number
  offset: number
  limit: number
  onChange: (offset: number) => void
}) {
  const end = Math.min(total, offset + limit)
  return (
    <div className="flex items-center justify-end gap-2 text-body-compact text-ink-subtle">
      <span className="num font-medium">
        {total === 0 ? 0 : fmt.int(offset + 1)}–{fmt.int(end)} of {fmt.int(total)}
      </span>
      <div className="flex items-center gap-1 rounded-sm border border-hairline bg-surface-1 p-0.5">
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Previous page"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          <ChevronLeftIcon />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Next page"
          disabled={end >= total}
          onClick={() => onChange(offset + limit)}
        >
          <ChevronRightIcon />
        </Button>
      </div>
    </div>
  )
}

/** A header with its hyphens made non-breaking, so ``Self-Correlation`` wraps at spaces only
 *  and never as ``Self-`` over ``Correlation``. */
function wholeWords(node: ReactNode): ReactNode {
  if (typeof node === 'string') return node.replaceAll('-', '\u2011')
  if (isValidElement<{ children?: ReactNode }>(node) && node.props.children !== undefined)
    return cloneElement(node, undefined, Children.map(node.props.children, wholeWords))
  return node
}
