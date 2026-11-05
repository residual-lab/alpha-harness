/** Stored alphas: the local vault, sorted and filtered in DuckDB, served a page at a time. */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { CopyIcon, SearchIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/api/http'
import { REGION_AGNOSTIC_REGION } from '@/api/platform'
import type { Scope } from '@/api/types'
import { CORE_ORDER, fmt } from '@/lib/format'
import { regionLabel } from '@/lib/scope'
import { useDebounced } from '@/lib/use-debounced'
import { useRefetchOn } from '@/lib/ws'
import { AlphaTable, fromStored } from '@/screens/alpha-table'
import { OPTIONS } from '@/screens/alpha-table/columns'
import {
  type AlphaMetricKey,
  type AlphaPageRequest,
  type AlphaSortKey,
  pool,
} from '@/screens/pool/api'
import { MAX_SEEDS, MIN_SEEDS, useSeedPick } from '@/screens/research-labs/evolution/seed-pick'
import {
  Button,
  Chips,
  Disclosure,
  ErrorNotice,
  Fieldset,
  Input,
  Metric,
  Panel,
  Segmented,
} from '@/ui/kit'
import { Pager, type Sort } from '@/ui/table'
import { alphasMarkdown } from './copy'
import { alphaIds } from './detail'

/** Bounds as the user types them; `div` converts the display unit to the wire fraction. */
const BOUNDS: { key: AlphaMetricKey; label: string; div: number }[] = [
  { key: 'sharpe', label: 'Sharpe', div: 1 },
  { key: 'fitness', label: 'Fitness', div: 1 },
  { key: 'turnover', label: 'Turnover %', div: 100 },
  { key: 'returns', label: 'Returns %', div: 100 },
  { key: 'drawdown', label: 'Drawdown %', div: 100 },
  { key: 'margin', label: 'Margin bps', div: 10_000 },
  { key: 'operator_count', label: 'Operators', div: 1 },
]

const OFFER = [
  'alphaId',
  'expression',
  'name',
  'dateCreated',
  'dateSubmitted',
  'classifications',
  'pyramids',
  'operatorCount',
  'hasPnl',
  'region',
  'delay',
  'universe',
  'investability',
  'neutralization',
  'decay',
  'truncation',
  ...CORE_ORDER,
  'trainSharpe',
  'testSharpe',
  'longCount',
  'shortCount',
  'imbalance',
]

const DEFAULTS = [
  'alphaId',
  'expression',
  'region',
  'delay',
  'universe',
  ...CORE_ORDER,
  'operatorCount',
  'dateCreated',
  'hasPnl',
]

const serverKey = (key: string) =>
  (OPTIONS.find((o) => o.key === key)?.serverKey ?? 'sharpe') as AlphaSortKey

/** Selection actions: clickable, so a lifted surface that brightens on hover, like pressed chips. */
const SELECTION_ACTION = 'border-hairline-strong bg-surface-2 text-ink hover:bg-surface-3'

const unique = <T,>(values: (T | null)[]) => [...new Set(values.filter((v): v is T => v != null))]

export function Stored({
  onOpen,
  openId,
}: {
  onOpen: (alphaId: string, ids?: string[]) => void
  openId: string | null
}) {
  const overview = useQuery({
    queryKey: ['pool', 'overview'],
    queryFn: pool.overview,
  })
  const [submitted, setSubmitted] = useState<'no' | 'yes'>('no')
  const [copying, setCopying] = useState(false)
  const [search, setSearch] = useState('')
  const [regions, setRegions] = useState<string[]>([])
  const [delays, setDelays] = useState<string[]>([])
  const [universes, setUniverses] = useState<string[]>([])
  const [bounds, setBounds] = useState<Record<string, { min: string; max: string }>>({})
  const [sort, setSort] = useState<Sort>({ key: 'sharpe', desc: true })
  const [limit, setLimit] = useState(100)
  const [offset, setOffset] = useState(0)
  // Debounced rather than deferred: `useDeferredValue` still asks the backend once per keystroke.
  const quietSearch = useDebounced(search, 300)
  const quietBounds = useDebounced(bounds, 400)
  // Picking seeds for Evolution Lab: only its market's unsubmitted Alphas, ticked into the pick.
  const pick = useSeedPick()
  const market = pick.active ? pick.scope : null
  const picked = useMemo(() => new Set(pick.ids), [pick.ids])

  /** Any filter change starts again from the first page. */
  const reset =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v)
      setOffset(0)
    }

  const body: AlphaPageRequest = {
    submitted: market ? false : submitted === 'yes',
    // While picking seeds, hide what the lab would refuse. A SuperAlpha's expression is a
    // combo, not a regular expression, so it can never be bred from — offering it here and
    // rejecting it on submit is the table misleading the user.
    evolvable: Boolean(market),
    sort_by: serverKey(sort.key),
    sort_desc: sort.desc,
    regions: market ? [market.region] : regions.length ? regions : null,
    delays: market ? [market.delay] : delays.length ? delays.map(Number) : null,
    universes: market ? [market.universe] : universes.length ? universes : null,
    search: quietSearch.trim() || null,
    minimum: Object.fromEntries(
      BOUNDS.flatMap((b) => {
        const v = parseNum(quietBounds[b.key]?.min ?? '')
        return v === undefined ? [] : [[b.key, v / b.div]]
      }),
    ),
    maximum: Object.fromEntries(
      BOUNDS.flatMap((b) => {
        const v = parseNum(quietBounds[b.key]?.max ?? '')
        return v === undefined ? [] : [[b.key, v / b.div]]
      }),
    ),
    limit,
    offset,
  }
  const page = useQuery({
    queryKey: ['pool', 'stored', body],
    queryFn: () => pool.query(body),
    placeholderData: keepPreviousData,
  })
  useRefetchOn('tasks', ['pool', 'stored'], 5000)
  // An Alpha simulated from the app is signalled here once it is stored locally.
  useRefetchOn('simulations', ['pool', 'stored'], 5000)
  const rows = useMemo(() => page.data?.results ?? [], [page.data])
  const shaped = useMemo(() => rows.map(fromStored), [rows])

  const scopes = overview.data?.scopes ?? []
  const regionItems = unique(scopes.map((s) => s.region)).map((v) => ({
    value: v,
    label: v,
  }))
  const delayItems = unique(scopes.map((s) => (s.delay == null ? null : String(s.delay)))).map(
    (v) => ({ value: v, label: <span className="num">D{v}</span> }),
  )
  const universeItems = unique(scopes.map((s) => s.universe)).map((v) => ({
    value: v,
    label: v,
  }))

  // Rows are ticked only to pick seeds; anywhere else a tick would lead nowhere.
  const toggle = (ids: string[], on: boolean) =>
    useSeedPick.setState((s) => ({
      ids: on ? [...new Set([...s.ids, ...ids])] : s.ids.filter((id) => !ids.includes(id)),
    }))
  const activeBounds =
    BOUNDS.filter((b) => bounds[b.key]?.min || bounds[b.key]?.max).length +
    (market ? 0 : regions.length + delays.length + universes.length)

  return (
    <>
      {market && <SeedPickBar market={market} />}
      <Panel title="Stored Alphas" bodyClassName="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-full max-w-sm">
            <SearchIcon
              className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-tertiary"
              aria-hidden
            />
            <Input
              aria-label="Search Alpha id, name or expression"
              placeholder="Search id, name or expression"
              className="num pl-8"
              value={search}
              onChange={(e) => reset(setSearch)(e.target.value)}
            />
          </div>
          {!market && (
            <Segmented
              label="Submitted"
              value={submitted}
              onChange={reset(setSubmitted)}
              items={[
                { value: 'no', label: 'Unsubmitted' },
                { value: 'yes', label: 'Submitted' },
              ]}
            />
          )}
          {!market && submitted === 'yes' && (
            <Button
              size="sm"
              variant="ghost"
              loading={copying}
              disabled={!page.data?.total}
              onClick={() => {
                setCopying(true)
                alphasMarkdown(body, 'Submitted Alphas')
                  .then((text) => navigator.clipboard.writeText(text))
                  .then(
                    () =>
                      toast.success(`Copied ${fmt.int(page.data?.total ?? 0)} Submitted Alphas`),
                    (e: unknown) => toast.error(errorMessage(e)),
                  )
                  .finally(() => setCopying(false))
              }}
            >
              {!copying && <CopyIcon />}
              Copy Alphas
            </Button>
          )}
        </div>

        <Disclosure summary={activeBounds ? `Filters (${activeBounds} active)` : 'Filters'}>
          <div className="flex flex-col gap-4">
            {!market && overview.isError && (
              <ErrorNotice error={overview.error} title="Could not read the markets to filter by" />
            )}
            {!market && (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                <Fieldset legend="Region">
                  <Chips
                    label="Region"
                    items={regionItems}
                    value={regions}
                    onChange={reset(setRegions)}
                  />
                </Fieldset>
                <Fieldset legend="Delay">
                  <Chips
                    label="Delay"
                    items={delayItems}
                    value={delays}
                    onChange={reset(setDelays)}
                  />
                </Fieldset>
                <Fieldset legend="Universe">
                  <Chips
                    label="Universe"
                    items={universeItems}
                    value={universes}
                    onChange={reset(setUniverses)}
                  />
                </Fieldset>
              </div>
            )}
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-4">
              {BOUNDS.map((b) => (
                <Fieldset key={b.key} legend={b.label}>
                  <div className="flex gap-1.5">
                    {(['min', 'max'] as const).map((side) => (
                      <Input
                        key={side}
                        type="number"
                        inputMode="decimal"
                        step="any"
                        aria-label={`${side === 'min' ? 'Minimum' : 'Maximum'} ${b.label}`}
                        placeholder={side}
                        value={bounds[b.key]?.[side] ?? ''}
                        onChange={(e) =>
                          reset(setBounds)({
                            ...bounds,
                            [b.key]: {
                              ...(bounds[b.key] ?? { min: '', max: '' }),
                              [side]: e.target.value,
                            },
                          })
                        }
                      />
                    ))}
                  </div>
                </Fieldset>
              ))}
            </div>
          </div>
        </Disclosure>

        {market && (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              className={SELECTION_ACTION}
              disabled={rows.length === 0}
              onClick={() =>
                toggle(
                  rows.map((r) => r.alphaId),
                  true,
                )
              }
            >
              Select page
            </Button>
            {picked.size > 0 && (
              <Button
                size="sm"
                className={SELECTION_ACTION}
                onClick={() => toggle([...picked], false)}
              >
                Clear
              </Button>
            )}
          </div>
        )}
        {page.isError && rows.length > 0 && (
          <ErrorNotice error={page.error} title="Could not read stored Alphas" />
        )}

        <AlphaTable
          view="stored"
          label="Stored Alphas"
          rows={shaped}
          offer={OFFER}
          defaults={DEFAULTS}
          serverSort
          page={offset}
          openAlpha={openId}
          onRowClick={(r, _event, shown) => r.alphaId && onOpen(r.alphaId, alphaIds(shown))}
          sort={sort}
          onSort={(s) => {
            setSort(s)
            setOffset(0)
          }}
          {...(market && {
            selected: picked,
            onSelect: toggle,
          })}
          loading={page.isPending || page.isPlaceholderData}
          error={page.error}
          empty={
            overview.data?.counts.alphas === 0
              ? 'No Alphas stored yet. Press Sync from BRAIN above.'
              : 'No Alphas match these filters.'
          }
        />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Segmented
            label="Rows per page"
            value={limit}
            onChange={reset(setLimit)}
            items={[100, 250, 500].map((v) => ({ value: v, label: String(v) }))}
          />
          <Pager total={page.data?.total ?? 0} offset={offset} limit={limit} onChange={setOffset} />
        </div>
      </Panel>
    </>
  )
}

/** While Evolution Lab picks seeds: how many are ticked, in which market, and the way back to it. */
function SeedPickBar({ market }: { market: Scope }) {
  const navigate = useNavigate()
  const count = useSeedPick((s) => s.ids.length)
  const back = (done: boolean) => {
    const pick = useSeedPick.getState()
    const to = pick.from
    if (done) pick.finish()
    else pick.cancel()
    void navigate({ to })
  }

  return (
    <div className="sticky top-0 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-hairline bg-surface-1 p-3">
      <Metric
        boxed
        size="sm"
        label="Seeds Selected"
        value={fmt.int(count)}
        tone={count > MAX_SEEDS ? 'warn' : 'neutral'}
      />
      <Metric
        boxed
        size="sm"
        label="Market"
        value={`${regionLabel(market.region)} · D${market.delay} · ${market.universe}`}
      />
      {market.region === REGION_AGNOSTIC_REGION ? (
        <span className="min-w-60 flex-1 text-body-compact text-pretty text-ink-subtle">
          Each region-agnostic Alpha shows the region it is scored on: its second-best, since BRAIN
          submits one only when two regions pass.
        </span>
      ) : (
        <span className="flex-1" />
      )}
      {count > MAX_SEEDS && (
        <span className="text-body-compact text-status-warning">
          At most {fmt.int(MAX_SEEDS)} seeds.
        </span>
      )}
      <Button variant="ghost" onClick={() => back(false)}>
        Cancel
      </Button>
      <Button
        variant="primary"
        disabled={count < MIN_SEEDS || count > MAX_SEEDS}
        onClick={() => back(true)}
      >
        Use in Evolution Lab
      </Button>
    </div>
  )
}

/** `undefined` for an empty or non-numeric input. */
const parseNum = (text: string): number | undefined => {
  const v = Number(text)
  return text.trim() === '' || !Number.isFinite(v) ? undefined : v
}
