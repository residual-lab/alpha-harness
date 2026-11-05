/**
 * The Sync matrix: every market BRAIN offers as a Region × Universe matrix, each
 * cell split into Delay 0 | Delay 1, filling live as a sync works through it. Click a half
 * to open that market in the Data Explorer.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCwIcon, XIcon } from 'lucide-react'
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { catalog } from '@/api/catalog'
import { REGION_AGNOSTIC_REGION } from '@/api/platform'
import type { Scope, SyncMarket, SyncRun } from '@/api/types'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { useLive } from '@/lib/live'
import { useNow } from '@/lib/now'
import { STAT } from '@/screens/data/state'
import { Button, Empty, ErrorNotice, Metric, Notice, Panel, Progress, Skeleton } from '@/ui/kit'
import { Confirm } from '@/ui/overlay'

type TileState = SyncMarket['state']

/** Each Delay half, by state. Fetching sweeps an ink shimmer across; synced settles solid. */
const HALF: Record<TileState, string> = {
  waiting: 'bg-surface-3',
  fetching:
    'animate-sweep bg-[length:200%_100%] bg-[linear-gradient(90deg,color-mix(in_srgb,var(--color-ink)_15%,transparent),color-mix(in_srgb,var(--color-ink)_70%,transparent),color-mix(in_srgb,var(--color-ink)_15%,transparent))]',
  fields: 'bg-ink-disabled',
  details: 'animate-pulse bg-ink-disabled ring-1 ring-ink ring-inset',
  done: 'bg-ink',
  failed: 'bg-pnl-negative-dim',
}

const LEGEND: [TileState, string][] = [
  ['waiting', 'Not Synced'],
  ['fetching', 'Fetching Fields'],
  ['fields', 'Fields Ready'],
  ['details', 'Filling Details'],
  ['done', 'Synced'],
  ['failed', 'Failed'],
]
const LABEL = Object.fromEntries(LEGEND) as Record<TileState, string>

const NO_DELAY =
  'bg-[repeating-linear-gradient(135deg,var(--color-hairline-strong)_0_1px,transparent_1px_5px)]'

const keyOf = (region: string, delay: number, universe: string) => `${region}|${delay}|${universe}`

/** Seconds between two ISO timestamps, or `null` while either is missing. */
function seconds(from: string | null | undefined, to: string | null | undefined): number | null {
  if (!from || !to) return null
  const span = (Date.parse(to) - Date.parse(from)) / 1000
  return Number.isFinite(span) && span >= 0 ? span : null
}

/** Counts up in its own component, so a running sync does not re-render the whole panel. */
function Elapsed({ since }: { since: string }) {
  const now = useNow(1000)
  return <span className="num text-ink">{fmt.duration((now - Date.parse(since)) / 1000)}</span>
}

/**
 * The live run, but only when it is the kind the asking panel owns.
 *
 * Both downloads report through the same run row, so a panel has to recognise its own: an
 * all-regions sync carries nothing but region ALL markets, and an ordinary one carries none.
 */
function ownRun(run: SyncRun | null | undefined, regionAgnostic: boolean) {
  const markets = run?.markets ?? []
  const mine =
    markets.length === 0
      ? !regionAgnostic
      : markets.every((m) => (m.region === REGION_AGNOSTIC_REGION) === regionAgnostic)
  const full = run?.all && mine ? run : null
  return { full, running: full?.status === 'RUNNING' ? full : null }
}

export function SyncHero({
  scope,
  onPick,
}: {
  scope: Scope
  onPick: (change: Partial<Scope>) => void
}) {
  const queryClient = useQueryClient()
  const markets = useQuery({
    queryKey: ['catalog', 'markets'],
    queryFn: catalog.markets,
    staleTime: 60 * 60 * 1000,
  })
  const scopes = useQuery({
    queryKey: ['catalog', 'scopes'],
    queryFn: catalog.scopes,
  })
  const size = useQuery({
    queryKey: ['catalog', 'size'],
    queryFn: catalog.size,
  })

  // Progress arrives on the socket; a finished run refreshes everything the catalog feeds.
  const run = useLive((s) => s.sync)
  const { full, running } = ownRun(run, false)
  const settled = run && run.status !== 'RUNNING' ? `${run.id}:${run.status}` : null
  // A run that had already ended when this screen opened is in the catalog the queries just
  // loaded; only a run settling under the open screen is news.
  const seen = useRef(settled)
  useEffect(() => {
    if (settled && seen.current !== settled) {
      seen.current = settled
      void queryClient.invalidateQueries({ queryKey: ['catalog'] })
    }
  }, [settled, queryClient])

  // The run being cancelled, held by id: it can finish on its own while the dialog is open.
  const [cancelId, setCancelId] = useState<number | null>(null)
  const cancel = useMutation({
    mutationFn: catalog.cancel,
    onSuccess: (result) => {
      toast.success(result.cancelled ? 'Sync cancelled' : 'The sync had already finished')
      void queryClient.invalidateQueries({ queryKey: ['catalog'] })
    },
    onSettled: () => setCancelId(null),
  })

  const layout = useMemo(() => {
    const list = markets.data ?? []
    const delays = [...new Set(list.map((m) => m.delay))].sort((a, b) => a - b)
    const universes = new Map<string, string[]>()
    for (const m of list) {
      const column = universes.get(m.region) ?? []
      if (!column.includes(m.universe)) column.push(m.universe)
      universes.set(m.region, column)
    }
    // Widest markets first, so the tallest columns lead and the grid fills from the left.
    const ordinary = [...universes.keys()]
      .filter((r) => r !== REGION_AGNOSTIC_REGION)
      .sort(
        (a, b) =>
          (universes.get(b)?.length ?? 0) - (universes.get(a)?.length ?? 0) || a.localeCompare(b),
      )
    // All regions is a different kind of market, not a wider one, so it is drawn in its own
    // panel rather than as a tenth column here.
    const ordinaryMarkets = list.filter((m) => m.region !== REGION_AGNOSTIC_REGION)
    const exists = new Set(list.map((m) => keyOf(m.region, m.delay, m.universe)))
    return { total: ordinaryMarkets.length, regions: ordinary, delays, universes, exists }
  }, [markets.data])

  // While a sync runs its own per-market states are the truth; otherwise what the catalog holds,
  // plus any market the last sync could not finish.
  const states = useMemo(() => {
    const map = new Map<string, { state: TileState; fields: number | null }>()
    for (const r of scopes.data ?? [])
      map.set(keyOf(r.region, r.delay, r.universe), {
        state: 'done',
        fields: r.fields,
      })
    for (const m of full?.markets ?? []) {
      const key = keyOf(m.region, m.delay, m.universe)
      if (running || m.state === 'failed' || m.state === 'fields')
        map.set(key, {
          state: m.state,
          fields: m.fields ?? map.get(key)?.fields ?? null,
        })
    }
    return map
  }, [scopes.data, full, running])

  // The all-regions market is counted in its own panel, never here, or the totals read as
  // more markets synced than this grid draws.
  const held = (scopes.data ?? []).filter((r) => r.region !== REGION_AGNOSTIC_REGION)
  const syncedMarkets = held.length
  const syncedFields = held.reduce((sum, r) => sum + r.fields, 0)
  const finished = seconds(full?.startedAt, full?.finishedAt)
  // From the catalog rather than the run, so it survives a restart with the other figures.
  const regionsSynced = new Set(held.map((r) => r.region)).size

  return (
    <Panel
      title="BRAIN Datasets"
      description={
        <span className="mt-1.5 flex flex-wrap items-center gap-2">
          {running ? (
            <>
              <span className={STAT}>
                {running.stage === 'details' ? 'Filling in Dataset Details' : 'Syncing Data Fields'}
              </span>
              <span className={STAT}>
                <span className="num text-ink">
                  {fmt.int(running.scopesDone)}/{fmt.int(running.scopesTotal)}
                </span>
                markets
              </span>
              <span className={STAT}>
                <span className="num text-ink">{fmt.pct(running.fraction, 0)}</span>
              </span>
              {running.startedAt && (
                <span className={STAT}>
                  <Elapsed since={running.startedAt} />
                </span>
              )}
            </>
          ) : (
            <>
              <span className={STAT}>
                <span className="num text-ink">{fmt.int(syncedMarkets)}</span>of
                <span className="num text-ink">{fmt.int(layout.total)}</span>
                markets synced
              </span>
              <span className={STAT}>
                <span className="num text-ink">{fmt.int(syncedFields)}</span>
                fields
              </span>
            </>
          )}
        </span>
      }
      actions={
        running ? (
          <Button
            variant="danger"
            size="sm"
            loading={cancel.isPending}
            onClick={() => setCancelId(running.id)}
          >
            {!cancel.isPending && <XIcon />}
            Cancel Sync
          </Button>
        ) : (
          <SyncButton variant="primary">Sync</SyncButton>
        )
      }
      bodyClassName="flex flex-col gap-4"
    >
      {markets.isError && (
        <ErrorNotice error={markets.error} title="Could not list BRAIN's markets" />
      )}
      {scopes.isError && (
        <ErrorNotice error={scopes.error} title="Could not read which markets are synced" />
      )}
      {full && !running && full.error && (
        <Notice
          tone={full.status === 'FAILED' ? 'error' : 'warn'}
          title={
            full.status === 'FAILED'
              ? 'The sync failed'
              : full.status === 'CANCELLED'
                ? 'The sync was cancelled'
                : 'Some markets did not sync, even after 3 tries'
          }
        >
          {full.error}
        </Notice>
      )}

      {markets.isPending ? (
        <Skeleton className="h-72" />
      ) : layout.total === 0 ? (
        !markets.isError && <Empty title="BRAIN offered no markets" />
      ) : (
        <div className="-m-1 overflow-x-auto p-1">
          <div
            role="group"
            aria-label="Sync matrix: Region by Universe, each split into Delay 0 and Delay 1"
            className="grid min-w-max gap-2"
            style={{
              gridTemplateColumns: `repeat(${layout.regions.length}, minmax(112px, 1fr))`,
            }}
          >
            {layout.regions.map((region) => (
              <div key={region} className="flex flex-col gap-1.5">
                <div className="flex min-w-0 items-baseline justify-between gap-2 px-1">
                  <span className="num min-w-0 truncate text-body font-medium text-ink">
                    {region}
                  </span>
                  <span className="num shrink-0 text-caption text-ink-subtle">
                    {layout.delays.map((d) => `D${d}`).join(' · ')}
                  </span>
                </div>
                {(layout.universes.get(region) ?? []).map((universe) => (
                  <div
                    key={universe}
                    className="flex flex-col gap-1 rounded-md border border-hairline bg-surface-2 p-1.5"
                  >
                    <span
                      className="num truncate px-0.5 text-caption text-ink-subtle"
                      title={universe}
                    >
                      {universe}
                    </span>
                    <div
                      className="grid gap-1"
                      style={{
                        gridTemplateColumns: `repeat(${layout.delays.length}, minmax(0, 1fr))`,
                      }}
                    >
                      {layout.delays.map((delay) => {
                        const key = keyOf(region, delay, universe)
                        if (!layout.exists.has(key)) {
                          return (
                            <span
                              key={delay}
                              title={`${region} · ${universe} has no Delay ${delay}`}
                              className={cn('h-5 rounded-xs', NO_DELAY)}
                            />
                          )
                        }
                        const tile = states.get(key) ?? {
                          state: 'waiting' as const,
                          fields: null,
                        }
                        const selected =
                          scope.region === region &&
                          scope.delay === delay &&
                          scope.universe === universe
                        // Not aria-pressed: this opens the market in the Data Explorer rather
                        // than toggling anything, so the ring's meaning goes in the label.
                        const label = `${region} · Delay ${delay} · ${universe}: ${LABEL[tile.state]}${tile.fields != null ? ` · ${fmt.int(tile.fields)} fields` : ''}${selected ? ' · shown in the Data Explorer' : ''}`
                        return (
                          <button
                            key={delay}
                            type="button"
                            title={label}
                            aria-label={label}
                            onClick={() => onPick({ region, delay, universe })}
                            className={cn(
                              'h-5 rounded-xs transition-colors hover:brightness-125',
                              HALF[tile.state],
                              selected && 'ring-2 ring-link ring-offset-1 ring-offset-surface-2',
                            )}
                          />
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      {running && <Progress value={running.fraction} label="Sync progress" />}

      {!running && finished !== null && full?.status === 'COMPLETE' && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric boxed label="Synced In" value={fmt.duration(finished)} />
          <Metric boxed label="Regions" value={fmt.int(regionsSynced || layout.regions.length)} />
          <Metric boxed label="Datasets" value={fmt.int(full.datasetsSynced)} />
          <Metric
            boxed
            label="Size"
            value={size.data ? `${fmt.int(size.data.used_bytes / 1e6)} MB` : DASH}
          />
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 text-body-compact text-ink-subtle">
        <div className="flex flex-wrap items-center gap-3">
          {LEGEND.map(([state, text]) => (
            <span key={state} className="flex items-center gap-1.5">
              <span className={cn('h-3 w-5 shrink-0 rounded-xs', HALF[state])} />
              {text}
            </span>
          ))}
          <span className="flex items-center gap-1.5">
            <span className={cn('h-3 w-5 shrink-0 rounded-xs', NO_DELAY)} />
            Not Offered
          </span>
        </div>
      </div>

      <Confirm
        open={cancelId !== null}
        onOpenChange={(open) => !open && setCancelId(null)}
        title="Cancel this sync?"
        confirmLabel="Cancel sync"
        cancelLabel="Keep syncing"
        danger
        pending={cancel.isPending}
        onConfirm={() => cancelId !== null && cancel.mutate(cancelId)}
      >
        Stops the sync. Markets that already arrived stay in the catalog; press Sync again to finish
        the rest.
      </Confirm>
    </Panel>
  )
}

/**
 * The region-agnostic market, on its own.
 *
 * One expression, run in USA, Europe, Asia and Global at once. BRAIN will not hand its fields
 * over the way it hands over every other market's — fifty at a time, dataset by dataset — so
 * it is a separate download, in a panel that says what it costs before you start it. Three
 * universes, one row: `ALL` has no Delay 0 and never will.
 */
export function RegionAgnosticHero({
  scope,
  onPick,
}: {
  scope: Scope
  onPick: (change: Partial<Scope>) => void
}) {
  const markets = useQuery({
    queryKey: ['catalog', 'markets'],
    queryFn: catalog.markets,
    staleTime: 60 * 60 * 1000,
  })
  const scopes = useQuery({ queryKey: ['catalog', 'scopes'], queryFn: catalog.scopes })
  const { full, running } = ownRun(
    useLive((s) => s.sync),
    true,
  )

  const [cancelId, setCancelId] = useState<number | null>(null)
  const cancel = useMutation({
    mutationFn: catalog.cancel,
    onSuccess: (result) =>
      toast.success(result.cancelled ? 'Sync cancelled' : 'The sync had already finished'),
    onSettled: () => setCancelId(null),
  })

  const universes = useMemo(
    () =>
      (markets.data ?? [])
        .filter((m) => m.region === REGION_AGNOSTIC_REGION)
        .map((m) => m.universe),
    [markets.data],
  )
  const held = useMemo(
    () =>
      new Map(
        (scopes.data ?? [])
          .filter((r) => r.region === REGION_AGNOSTIC_REGION)
          .map((r) => [r.universe, r.fields]),
      ),
    [scopes.data],
  )
  const live = useMemo(
    () =>
      new Map<string, SyncMarket>(
        (full?.markets ?? [])
          .filter((m) => m.region === REGION_AGNOSTIC_REGION)
          .map((m) => [m.universe, m] as const),
      ),
    [full],
  )

  // BRAIN lists region ALL only for an account that may simulate region-agnostically, so an
  // empty market list is the permission answer: nothing to offer, nothing to show.
  if (universes.length === 0) return null

  const syncedFields = [...held.values()].reduce((sum, n) => sum + n, 0)

  return (
    <Panel
      title="Sync Region Agnostic Data"
      description={
        <span className="mt-1.5 flex flex-wrap items-center gap-2">
          <span className={STAT}>
            <span className="num text-ink">{fmt.int(held.size)}</span>of
            <span className="num text-ink">{fmt.int(universes.length)}</span>
            universes synced
          </span>
          <span className={STAT}>
            <span className="num text-ink">{fmt.int(syncedFields)}</span>
            fields
          </span>
          {running?.startedAt && (
            <span className={STAT}>
              <Elapsed since={running.startedAt} />
            </span>
          )}
        </span>
      }
      actions={
        running ? (
          <Button
            variant="danger"
            size="sm"
            loading={cancel.isPending}
            onClick={() => setCancelId(running.id)}
          >
            {!cancel.isPending && <XIcon />}
            Cancel Sync
          </Button>
        ) : (
          <SyncButton regionAgnostic>Sync Region Agnostic Data</SyncButton>
        )
      }
      bodyClassName="flex flex-col gap-4"
    >
      <div className="grid gap-2 sm:grid-cols-3">
        {universes.map((universe) => {
          const state: TileState =
            live.get(universe)?.state ?? (held.has(universe) ? 'done' : 'waiting')
          const fields = live.get(universe)?.fields ?? held.get(universe) ?? null
          const selected = scope.region === REGION_AGNOSTIC_REGION && scope.universe === universe
          const label = `All Regions · Delay 1 · ${universe}: ${LABEL[state]}${fields != null ? ` · ${fmt.int(fields)} fields` : ''}${selected ? ' · shown in the Data Explorer' : ''}`
          return (
            <button
              key={universe}
              type="button"
              title={label}
              aria-label={label}
              onClick={() => onPick({ region: REGION_AGNOSTIC_REGION, delay: 1, universe })}
              className={cn(
                'flex flex-col gap-1.5 rounded-md border border-hairline bg-surface-2 p-2 text-left transition-colors hover:border-hairline-strong',
                selected && 'ring-2 ring-link ring-offset-1 ring-offset-surface-1',
              )}
            >
              <span className="flex items-baseline justify-between gap-2">
                <span className="num truncate text-body text-ink">{universe}</span>
                <span className="num shrink-0 text-caption text-ink-subtle">
                  {fields != null ? fmt.int(fields) : DASH}
                </span>
              </span>
              <span className={cn('h-5 rounded-xs', HALF[state])} />
            </button>
          )
        })}
      </div>

      {running && <Progress value={running.fraction} label="Region-agnostic sync progress" />}

      {full && !running && full.error && (
        <Notice
          tone={full.status === 'FAILED' ? 'error' : 'warn'}
          title={full.status === 'CANCELLED' ? 'The sync was cancelled' : 'The sync did not finish'}
        >
          {full.error}
        </Notice>
      )}

      <Confirm
        open={cancelId !== null}
        onOpenChange={(open) => !open && setCancelId(null)}
        title="Cancel this sync?"
        confirmLabel="Cancel sync"
        cancelLabel="Keep syncing"
        danger
        pending={cancel.isPending}
        onConfirm={() => cancelId !== null && cancel.mutate(cancelId)}
      >
        Stops the download. Universes that already arrived stay in the catalog.
      </Confirm>
    </Panel>
  )
}

/** Start a catalog download. Calls BRAIN, spends no simulations. */
function useDownload(regionAgnostic: boolean) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => (regionAgnostic ? catalog.syncRegionAgnostic() : catalog.syncAll()),
    onSuccess: () => {
      toast.success(
        regionAgnostic ? 'Syncing the all-regions market' : 'Syncing every BRAIN Dataset',
      )
      void queryClient.invalidateQueries({ queryKey: ['catalog'] })
    },
  })
}

/** The one sync button: every market's data fields, then dataset details. Asks first, since it runs against BRAIN for minutes. */
export function SyncButton({
  children,
  variant = 'secondary',
  size = 'sm',
  regionAgnostic = false,
}: {
  children: ReactNode
  variant?: 'primary' | 'secondary'
  size?: 'sm' | 'md'
  /** Include region ALL, which BRAIN serves fifty fields at a time. */
  regionAgnostic?: boolean
}) {
  const [open, setOpen] = useState(false)
  const sync = useDownload(regionAgnostic)
  return (
    <>
      <Button variant={variant} size={size} loading={sync.isPending} onClick={() => setOpen(true)}>
        {!sync.isPending && <RefreshCwIcon />}
        {children}
      </Button>
      <Confirm
        open={open}
        onOpenChange={setOpen}
        title={regionAgnostic ? 'Sync Region Agnostic Data' : 'Sync BRAIN Datasets'}
        confirmLabel="Sync"
        pending={sync.isPending}
        onConfirm={() => sync.mutate(undefined, { onSettled: () => setOpen(false) })}
      >
        {regionAgnostic
          ? 'The Data Fields an alpha can use when it runs in every region at once: 139 Datasets, about 28,000 fields per universe. BRAIN serves this market fifty fields at a time, so it takes around ten minutes per universe — the rest of the app keeps working meanwhile.'
          : 'Downloads the Data Fields of every market BRAIN offers, then fills in Dataset Details. Fields are browsable in the Data Explorer as soon as they arrive; progress shows here in Sync.'}
      </Confirm>
    </>
  )
}
