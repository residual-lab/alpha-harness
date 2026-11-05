/**
 * The one Alpha table. Every screen that lists Alphas uses it, so each gets the same columns,
 * the same column picker, sorting, angles and After-Cost Mode. Each screen keeps its own choice
 * of columns, under its own `view` name.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Columns3Icon } from 'lucide-react'
import { type ReactNode, useEffect, useMemo, useState } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { components } from '@/api/generated'
import { http } from '@/api/http'
import { fmt } from '@/lib/format'
import { useDebounced } from '@/lib/use-debounced'
import { AlphaActionsMenu } from '@/screens/pool/shared'
import { Button, Checkbox, Input, Switch } from '@/ui/kit'
import { Dialog } from '@/ui/overlay'
import { type Column, DataTable, type Sort } from '@/ui/table'
import { ToggleTitle } from '@/ui/tile'
import { AnglesContext, GROUPS, NET_OF_COST, OPTIONS, type Option, sortValue } from './columns'
import type { AlphaTableRow } from './row'

export { type Option, option } from './columns'
export * from './row'

type NetFigures = components['schemas']['NetFigures']

interface View {
  shown: string[]
  /** Correlations shown as the angle ``arccos(rho)`` between the two PnL series. */
  angles: boolean
  /** After-Cost Mode: figures worked out again on PnL net of ``bps``. */
  afterCost: boolean
  bps: number
}

/** Portfolio's choice from before every table shared one store. */
function legacyPortfolio(): Partial<View> | undefined {
  try {
    const saved = localStorage.getItem('alpha-harness-portfolio-columns')
    return saved ? (JSON.parse(saved) as { state?: Partial<View> }).state : undefined
  } catch {
    return undefined
  }
}

const useViews = create<{ views: Record<string, Partial<View>> }>()(
  persist(
    () => {
      const portfolio = legacyPortfolio()
      return { views: portfolio ? { portfolio } : {} }
    },
    { name: 'alpha-harness-alpha-tables' },
  ),
)

const setView = (id: string, patch: Partial<View>) =>
  useViews.setState((s) => ({ views: { ...s.views, [id]: { ...s.views[id], ...patch } } }))

const DEFAULT_BPS = 5

/** The cost a table charges, in bps. */
export const useCostBps = (view: string) => useViews((s) => s.views[view]?.bps ?? DEFAULT_BPS)

/** The cost a table's figures are net of, or ``undefined`` while After-Cost Mode is off. */
export function useNetBps(view: string): number | undefined {
  const on = useViews((s) => s.views[view]?.afterCost ?? false)
  const bps = useCostBps(view)
  return on && bps > 0 ? bps : undefined
}

/** The cost input, wherever a screen offers it. Out-of-range or empty text snaps back to the
 *  cost actually used once the field is left, so it never shows a number that is not. */
export function CostInput({ view, disabled }: { view: string; disabled?: boolean }) {
  const bps = useCostBps(view)
  const [text, setText] = useState(String(bps))
  useEffect(() => {
    setText((current) => (Number(current) === bps ? current : String(bps)))
  }, [bps])
  return (
    <Input
      type="number"
      min={0}
      max={100}
      step={0.5}
      value={text}
      disabled={disabled}
      onChange={(e) => {
        setText(e.target.value)
        const value = Number(e.target.value)
        if (e.target.value.trim() !== '' && Number.isFinite(value))
          setView(view, { bps: Math.min(100, Math.max(0, value)) })
      }}
      onBlur={() => setText(String(bps))}
      aria-label="Cost in bps"
      className="w-20"
    />
  )
}

/** The figures the backend works out net of cost from a stored daily PnL. */
const LOCAL_NET: (keyof NetFigures & keyof AlphaTableRow)[] = [
  'sharpe',
  'fitness',
  'returns',
  'drawdown',
  'margin',
  'trainSharpe',
  'testSharpe',
  'kRatio',
]

/** Net figures for the Alphas whose daily PnL is stored; the rest get none, never a request. */
function useLocalNet(view: string, rows: AlphaTableRow[], on: boolean) {
  const bps = useDebounced(useNetBps(view), 400)
  const ids = useMemo(
    () => [...new Set(rows.flatMap((r) => (r.alphaId ? [r.alphaId] : [])))].sort(),
    [rows],
  )
  const charged = on ? bps : undefined
  const query = useQuery({
    queryKey: ['after-cost', ids, charged ?? null],
    queryFn: () =>
      http.post<components['schemas']['AfterCost']>('/api/vault/alphas/after-cost', {
        alpha_ids: ids,
        cost_bps: charged,
      }),
    enabled: charged !== undefined && ids.length > 0,
    placeholderData: keepPreviousData,
  })
  const merged = useMemo(() => {
    if (charged === undefined) return rows
    const figures = query.data?.figures ?? {}
    return rows.map((r) => {
      const net = r.alphaId ? figures[r.alphaId] : undefined
      const merged = { ...r }
      for (const key of LOCAL_NET) merged[key] = net?.[key] ?? null
      return merged
    })
  }, [rows, charged, query.data])
  return { rows: merged, pending: query.isFetching }
}

/** Rows in a column's order, blank last whichever way: an Alpha with no figure is not the lowest. */
export function sortAlphas<T extends AlphaTableRow>(
  rows: T[],
  sort: Sort | null,
  overrides: Option[] = [],
  angles = false,
): T[] {
  const by =
    sort && (overrides.find((o) => o.key === sort.key) ?? OPTIONS.find((o) => o.key === sort.key))
  if (!sort || !by?.value) return rows
  return [...rows].sort((a, b) => {
    const x = sortValue(by, a, angles)
    const y = sortValue(by, b, angles)
    if (x == null) return y == null ? 0 : 1
    if (y == null) return -1
    const order = typeof x === 'string' ? x.localeCompare(String(y)) : Number(x) - Number(y)
    return sort.desc ? -order : order
  })
}

export function AlphaTable({
  view,
  label,
  rows,
  offer,
  defaults,
  overrides = [],
  afterCost = 'local',
  sort,
  onSort,
  serverSort = false,
  selected,
  onSelect,
  onRowClick,
  rowClass,
  loading,
  error,
  empty,
  maxHeight,
  page,
  openAlpha,
}: {
  /** Where this screen's choice of columns is kept. */
  view: string
  label: string
  rows: AlphaTableRow[]
  /** The columns this screen's Alphas have, by key. */
  offer: string[]
  /** The ones shown until the reader chooses. */
  defaults: string[]
  /** A screen's own take on a column, replacing the shared one with the same key. */
  overrides?: Option[]
  /** Where net figures come from: the screen's own query, or the stored daily PnL. */
  afterCost?: 'screen' | 'local'
  /** Controlled when `onSort` is given; otherwise the table sorts on its own, from unsorted. */
  sort?: Sort | null
  onSort?: (sort: Sort) => void
  /** The backend sorts: only columns it knows sort, and the rows arrive in order. */
  serverSort?: boolean
  selected?: ReadonlySet<string>
  onSelect?: (keys: string[], on: boolean) => void
  onRowClick?: (
    row: AlphaTableRow,
    event: React.MouseEvent | React.KeyboardEvent,
    shown: AlphaTableRow[],
  ) => void
  /** Given the rows in the order shown, so a mark can depend on its neighbours. */
  rowClass?: (shown: AlphaTableRow[]) => (row: AlphaTableRow) => string | undefined
  loading?: boolean
  error?: unknown
  empty?: ReactNode
  maxHeight?: string
  /** The page shown, when paged: see the table's own. */
  page?: number
  /** The Alpha open in the detail sheet, marked in the table. */
  openAlpha?: string | null | undefined
}) {
  const saved = useViews((s) => s.views[view])
  const shown = new Set(saved?.shown ?? defaults)
  const angles = saved?.angles ?? false
  const options = OPTIONS.filter((o) => offer.includes(o.key)).map(
    (o) => overrides.find((x) => x.key === o.key) ?? o,
  )

  const { rows: netRows, pending: netPending } = useLocalNet(view, rows, afterCost === 'local')
  const netBps = useNetBps(view)
  const [ownSort, setOwnSort] = useState<Sort | null>(null)
  const current = onSort ? (sort ?? null) : ownSort
  const sorted = useMemo(
    () => (serverSort ? netRows : sortAlphas(netRows, current, overrides, angles)),
    [netRows, current, overrides, serverSort, angles],
  )

  const columns: Column<AlphaTableRow>[] = [
    ...options
      .filter((o) => shown.has(o.key))
      .map((o) => ({
        ...o.column,
        sortable: serverSort ? Boolean(o.serverKey) : o.value !== undefined,
      })),
    {
      key: 'actions',
      header: (
        <ColumnsButton
          view={view}
          options={options}
          shown={shown}
          defaults={defaults}
          netKeys={afterCost === 'local' ? new Set<string>(LOCAL_NET) : NET_OF_COST}
        />
      ),
      width: '52px',
      align: 'right',
      cell: (r) => r.alphaId && <AlphaActionsMenu alphaId={r.alphaId} links />,
    },
  ]

  return (
    <AnglesContext.Provider value={angles}>
      <div className="flex min-w-0 flex-col gap-2">
        {/* Each way the table is read other than as BRAIN sends it, said on the table itself
          rather than only in the column picker that turned it on. */}
        {(netBps !== undefined || angles) && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-body-compact text-ink-muted">
            {netBps !== undefined && (
              <span className="inline-flex flex-wrap items-center gap-1.5">
                After-Cost Mode · <span className="num text-ink">{fmt.ratio(netBps, 1)} bps</span>
                {afterCost === 'local' && (
                  <span className="text-ink-subtle">· — where no daily PnL is stored</span>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setView(view, { afterCost: false })}
                >
                  Turn Off
                </Button>
              </span>
            )}
            {angles && (
              <span className="inline-flex items-center gap-1.5">
                Correlations as Angles
                <Button size="sm" variant="ghost" onClick={() => setView(view, { angles: false })}>
                  Show ρ
                </Button>
              </span>
            )}
          </div>
        )}
        <DataTable
          label={label}
          rows={sorted}
          rowKey={(r) => r.key}
          activeKey={openAlpha ? sorted.find((r) => r.alphaId === openAlpha)?.key : null}
          columns={columns}
          {...(current ? { sort: current } : {})}
          onSort={onSort ?? setOwnSort}
          {...(selected && onSelect ? { selected, onSelect } : {})}
          {...(onRowClick ? { onRowClick } : {})}
          {...(rowClass ? { rowClass: rowClass(sorted) } : {})}
          loading={(loading ?? false) || netPending}
          error={error}
          {...(empty !== undefined ? { empty } : {})}
          maxHeight={maxHeight}
          {...(page !== undefined ? { page } : {})}
        />
      </div>
    </AnglesContext.Provider>
  )
}

/** Opens the column picker, with After-Cost Mode and angles beneath it. */
function ColumnsButton({
  view,
  options,
  shown,
  defaults,
  netKeys,
}: {
  view: string
  options: Option[]
  shown: ReadonlySet<string>
  defaults: string[]
  netKeys: ReadonlySet<string>
}) {
  const [open, setOpen] = useState(false)
  const saved = useViews((s) => s.views[view])
  const afterCost = useNetBps(view) !== undefined
  const set = (keys: string[], on: boolean) =>
    setView(view, {
      shown: on ? [...new Set([...shown, ...keys])] : [...shown].filter((k) => !keys.includes(k)),
    })
  const groups = GROUPS.filter((g) => options.some((o) => o.group === g))

  return (
    <>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Choose columns"
        title="Choose columns"
        onClick={() => setOpen(true)}
      >
        <Columns3Icon />
      </Button>
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="Select Columns"
        description="A group's title picks or clears the whole group."
        className="max-w-4xl"
        footer={
          <>
            <span className="mr-auto self-center text-body-compact text-ink-muted">
              * Computed Locally
            </span>
            <Button variant="ghost" onClick={() => setView(view, { shown: defaults })}>
              Reset
            </Button>
            <Button variant="primary" onClick={() => setOpen(false)}>
              Done
            </Button>
          </>
        }
      >
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {groups.map((group) => {
            const mine = options.filter((o) => o.group === group)
            const keys = mine.map((o) => o.key)
            const any = keys.some((k) => shown.has(k))
            return (
              <section key={group} className="flex flex-col gap-2.5">
                <h4 className="text-body-compact font-medium text-ink-muted">
                  <ToggleTitle label={group} any={any} onToggle={() => set(keys, !any)} />
                </h4>
                {mine.map((o) => (
                  <Checkbox
                    key={o.key}
                    label={
                      afterCost && netKeys.has(o.key) ? (
                        <span className="text-pnl-positive-text">{o.label}</span>
                      ) : (
                        o.label
                      )
                    }
                    checked={shown.has(o.key)}
                    onChange={(on) => set([o.key], on)}
                  />
                ))}
              </section>
            )
          })}
        </div>
        <div className="mt-5 flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-hairline-strong bg-surface-2 p-3">
            <Switch
              checked={saved?.afterCost ?? false}
              onChange={(on) => setView(view, { afterCost: on })}
              aria-label="After-Cost Mode"
            />
            <span className="text-body text-ink">After-Cost Mode</span>
            <CostInput view={view} disabled={!saved?.afterCost} />
            <span className="text-body text-ink-muted">bps</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-hairline-strong bg-surface-2 p-3">
            <Switch
              checked={saved?.angles ?? false}
              onChange={(on) => setView(view, { angles: on })}
              aria-label="Show Correlation as Angles"
            />
            <span className="text-body text-ink">Show Correlation as Angles</span>
          </div>
        </div>
      </Dialog>
    </>
  )
}
