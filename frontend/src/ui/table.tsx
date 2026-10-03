/**
 * A virtualised table: only visible rows mount, so a 500-row page scrolls without layout cost.
 * Sorting and paging are the backend's job (DuckDB); this only asks.
 */

import { useVirtualizer } from '@tanstack/react-virtual'
import { ArrowDownIcon, ArrowUpIcon, ChevronLeftIcon, ChevronRightIcon } from 'lucide-react'
import { type ReactNode, useRef } from 'react'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { Button, Empty, ErrorNotice, Skeleton } from './kit'

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
  onRowClick,
  rowClass,
  sort,
  onSort,
  selected,
  onSelect,
  onSelectAll,
  loading,
  error,
  empty = 'Nothing to show.',
  maxHeight = '70vh',
  rowHeight = 34,
  label,
  header: showHeader = true,
}: {
  rows: T[]
  columns: Column<T>[]
  rowKey: (row: T) => string
  /** The event comes too, so a table can treat a modified click as its own action. */
  onRowClick?: (row: T, event: React.MouseEvent | React.KeyboardEvent) => void
  /** Extra classes for one row, so a table can mark rows that mean something. */
  rowClass?: (row: T) => string | undefined
  sort?: Sort
  onSort?: (sort: Sort) => void
  /** With `onSelect`, adds a checkbox column. */
  selected?: ReadonlySet<string>
  onSelect?: (key: string, on: boolean) => void
  /** Adds a checkbox to the header that ticks or clears every row shown. */
  onSelectAll?: (on: boolean) => void
  loading?: boolean
  /** A failed query: shown in place of the empty state, which would claim there is nothing. */
  error?: unknown
  empty?: ReactNode
  /** Explicitly `undefined` keeps the default, so a caller can size it only sometimes. */
  maxHeight?: string | undefined
  rowHeight?: number
  label: string
  /** False drops the column names, for a table stacked under another with the same columns. */
  header?: boolean
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  })
  const selectable = selected !== undefined && onSelect !== undefined
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
        {selectable &&
          (onSelectAll ? (
            <div role="columnheader" className="flex items-center justify-center">
              <input
                type="checkbox"
                aria-label="Select every row shown"
                className="size-3.5"
                disabled={rows.length === 0}
                checked={rows.length > 0 && rows.every((row) => selected.has(rowKey(row)))}
                onChange={(e) => onSelectAll(e.target.checked)}
              />
            </div>
          ) : (
            <div role="columnheader" aria-label="Select" />
          ))}
        {columns.map((column) => {
          const active = sort?.key === column.key
          const Arrow = sort?.desc ? ArrowDownIcon : ArrowUpIcon
          const content = (
            // Headers wrap rather than clip: a long name would otherwise widen its track until
            // the whole table needed a horizontal scrollbar to show short values. The sort arrow
            // sits *inside* the text, so it wraps with the last word instead of being a rigid
            // sibling that collides with it in a narrow column.
            <span className={cn('min-w-0 text-balance', column.align === 'right' && 'text-right')}>
              {column.header}
              {active && <Arrow className="ml-1 inline size-3 align-text-bottom text-primary" />}
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
        {showHeader && header}
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
          <div role="rowgroup" className="relative" style={{ height: virtual.getTotalSize() }}>
            {virtual.getVirtualItems().map((item) => {
              const row = rows[item.index]
              if (row === undefined) return null
              const key = rowKey(row)
              const isSelected = selectable && selected.has(key)
              return (
                <div
                  key={key}
                  role="row"
                  aria-rowindex={item.index + 2}
                  aria-selected={selectable ? isSelected : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                  onClick={onRowClick ? (e) => onRowClick(row, e) : undefined}
                  onKeyDown={
                    onRowClick
                      ? (e) => {
                          if (e.key !== 'Enter' && e.key !== ' ') return
                          e.preventDefault()
                          onRowClick(row, e)
                        }
                      : undefined
                  }
                  className={cn(
                    'absolute inset-x-0 grid border-b border-hairline-subtle bg-surface-1 text-body-compact transition-colors',
                    onRowClick &&
                      'cursor-pointer hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:-outline-offset-2',
                    isSelected && 'bg-surface-2',
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
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    >
                      <input
                        type="checkbox"
                        aria-label="Select row"
                        className="size-3.5"
                        checked={isSelected}
                        onChange={(e) => onSelect(key, e.target.checked)}
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
