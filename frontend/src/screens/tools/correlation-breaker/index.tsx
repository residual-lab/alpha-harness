/**
 * Correlation Breaker (Tools): an Alpha BRAIN says is already in the production pool, re-shaped.
 *
 * The settings are the Alpha's own and are shown as read-only, because the whole idea is to
 * hold everything it was judged on and change only what it is exposed to. Every re-shape is
 * printed in full before anything is queued: a consultant should be able to read what will run.
 * Re-shapes of the user's own sit beside the built-in ones, kept on this computer for every
 * Alpha, and a built-in one can be copied to edit.
 */

import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useSearch } from '@tanstack/react-router'
import { PencilIcon, PlusIcon, Trash2Icon, UnlinkIcon } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { PROD_CORRELATION_CEILING } from '@/api/platform'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { followOtherTabs } from '@/lib/tabs'
import { useDebounced } from '@/lib/use-debounced'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import { CoresSetting } from '@/screens/research-labs/task-settings'
import { AlphaPanel, HeldMetrics } from '@/screens/tools/one-alpha'
import {
  Button,
  Checkbox,
  Empty,
  Input,
  LINK,
  Notice,
  Page,
  PageHeader,
  Panel,
  Skeleton,
  Textarea,
} from '@/ui/kit'
import { correlationBreaker, type Reshape } from './api'

/** The user's own re-shapes, kept on this computer and offered for every Alpha. */
const useReshapes = create<{ reshapes: Reshape[] }>()(
  persist(() => ({ reshapes: [] as Reshape[] }), {
    name: 'alpha-harness-correlation-breaker',
    version: 1,
  }),
)
followOtherTabs(useReshapes)

const CUSTOM = 'custom:'

function updateReshape(id: string, change: Partial<Reshape>) {
  useReshapes.setState((s) => ({
    reshapes: s.reshapes.map((r) => (r.id === id ? { ...r, ...change } : r)),
  }))
}

function addReshape(name: string, code: string) {
  useReshapes.setState((s) => ({
    reshapes: [...s.reshapes, { id: crypto.randomUUID(), name, code }],
  }))
}

function removeReshape(id: string) {
  useReshapes.setState((s) => ({ reshapes: s.reshapes.filter((r) => r.id !== id) }))
}

export function CorrelationBreakerScreen() {
  const search = useSearch({ from: '/tools/correlation-breaker' })
  const navigate = useNavigate()
  // The URL owns which Alpha is open, so a link from the Alpha screen is shareable.
  const alphaId = search.alpha ?? ''
  const [draft, setDraft] = useState(alphaId)
  const [chosen, setChosen] = useState<ReadonlySet<string>>(new Set())
  /** `null` until chosen here: until then Settings' default for new tasks applies. */
  const [chosenCores, setCores] = useState<number | null>(null)
  const cores = useCores(chosenCores)

  useEffect(() => setDraft(search.alpha ?? ''), [search.alpha])

  const reshapes = useReshapes((s) => s.reshapes)
  // Checked as typed, but not on every keystroke.
  const custom = useDebounced(reshapes, 400)
  const query = useQuery({
    queryKey: ['correlation-breaker', alphaId, custom],
    queryFn: () => correlationBreaker.preview({ alphaId, recipes: [], custom, cores: 1 }),
    enabled: alphaId.length > 0,
    retry: false,
    placeholderData: (previous, last) => (last?.queryKey[1] === alphaId ? previous : undefined),
  })
  const plan = query.data
  const runnable = useMemo(() => (plan?.recipes ?? []).filter((r) => !r.blocked), [plan])
  // Only what can run is sent: a ticked re-shape since edited into one that cannot is left out.
  const picked = runnable.filter((r) => chosen.has(r.id)).map((r) => r.id)

  // Each re-shape is ticked when first offered, the point being to try them, and left as the
  // reader set it after. One that would cost the Alpha Power Pool or single dataset status,
  // or that has only ever failed here, waits to be asked for.
  const offered = useRef({ alpha: '', ids: new Set<string>() })
  useEffect(() => {
    if (offered.current.alpha !== alphaId) {
      offered.current = { alpha: alphaId, ids: new Set() }
      setChosen(new Set())
    }
    const { ids } = offered.current
    const fresh = runnable.filter((r) => !ids.has(r.id))
    if (!fresh.length) return
    for (const r of fresh) ids.add(r.id)
    setChosen((prev) => {
      const next = new Set(prev)
      for (const r of fresh)
        if (!r.overPowerPool && !r.overSingleDataset && !r.gaveUp) next.add(r.id)
      return next
    })
  }, [runnable, alphaId])

  const analyse = (event: React.FormEvent) => {
    event.preventDefault()
    void navigate({
      to: '/tools/correlation-breaker',
      search: { alpha: draft.trim() || undefined },
      replace: true,
    })
  }

  const add = useAddTask(() =>
    correlationBreaker.addTask({ alphaId, recipes: picked, custom: reshapes, cores }),
  )
  // A typed ID that has not been analysed is not the Alpha the re-shapes below were read from.
  const analysed = draft.trim() === alphaId

  const toggle = (id: string) =>
    setChosen((prev) => {
      const next = new Set(prev)
      if (!next.delete(id)) next.add(id)
      return next
    })

  return (
    <Page>
      <PageHeader
        sticky
        title="Correlation Breaker"
        description="Re-shape an Alpha that is already in the Production Pool"
        actions={
          <AddTaskButtons
            add={add}
            disabled={picked.length === 0 || !analysed}
            blocked={!analysed ? 'Analyse an Alpha first.' : 'Tick a recipe to run.'}
          />
        }
      />

      <AlphaPanel
        draft={draft}
        setDraft={setDraft}
        onAnalyse={analyse}
        error={query.isError ? query.error : null}
        problems={plan?.problems}
      />

      {alphaId && query.isPending && <Skeleton className="h-64" label="Reading the Alpha" />}

      {plan?.expression && (
        // Read for the Alpha last analysed: faded once another ID is typed, until it is analysed.
        <div
          className={cn(
            'flex flex-col gap-4 transition-opacity',
            !analysed && 'pointer-events-none opacity-50',
          )}
        >
          <Panel
            title="Held Fixed"
            description="Every re-shape runs at the Alpha's own settings. Nothing here changes."
          >
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                <HeldMetrics settings={plan.settings} />
              </div>
              {/* The biggest lever of all is a settings change, which this tool deliberately
                  refuses to make. Saying so beats letting it be discovered. */}
              <Notice tone="info" title="A Neutralization sweep is the other half of this">
                Re-running the same expression at RAM, Statistical or Crowding neutralization often
                breaks correlation on its own, and needs no new expression at all. That is a
                Simulation Settings change, so it belongs to the{' '}
                <Link className={LINK} to="/tools/settings-sampler" search={{ alpha: alphaId }}>
                  Settings Sampler
                </Link>
                . Worth trying first, and it costs the same quota.
              </Notice>
              {plan.correlation && (
                <Notice
                  tone="warn"
                  title={`BRAIN last reported Production Correlation as ${String(
                    plan.correlation['result'] ?? 'PENDING',
                  )}`}
                >
                  {typeof plan.correlation['value'] === 'number' && (
                    <>
                      Its value was{' '}
                      <span className="num">{fmt.ratio(plan.correlation['value'], 4)}</span> against
                      a limit of{' '}
                      <span className="num">
                        {fmt.ratio(Number(plan.correlation['limit'] ?? PROD_CORRELATION_CEILING))}
                      </span>
                      .
                    </>
                  )}
                </Notice>
              )}
            </div>
          </Panel>

          <Panel
            title="Re-shapes"
            description="Each one wraps the Alpha below, read as alpha. Only the re-shape is shown; the binding is the same for all of them. Add your own, or copy a built-in one to edit; yours are kept on this computer for every Alpha."
            actions={<CoresSetting value={cores} onChange={setCores} />}
          >
            {plan.powerPool && (
              <Notice tone="info" className="mb-4" title="A Power Pool Alpha">
                Its expression has <span className="num">{fmt.int(plan.operators)}</span> operators
                and <span className="num">{fmt.int(plan.dataFields)}</span> data fields; Power Pool
                allows 8 and 3. A re-shape over either limit is left unticked, because it would no
                longer be a Power Pool Alpha.
              </Notice>
            )}
            {plan.singleDataset && (
              <Notice tone="info" className="mb-4" title="A Single Data Set Alpha">
                It reads one dataset. A re-shape that brings in a field of another dataset, such as
                cap or returns from Price Volume, is left unticked, because it would no longer be a
                Single Data Set Alpha. Grouping fields like sector and industry do not count.
              </Notice>
            )}
            {/* Said once, so a dozen cards do not repeat the same two hundred characters. */}
            <pre className="num mb-4 overflow-x-auto rounded-sm border border-hairline bg-canvas p-3 text-body-compact whitespace-pre text-ink-muted">
              {plan.bound}
            </pre>
            {plan.recipes.length === 0 ? (
              <Empty title="Nothing to re-shape" />
            ) : (
              <ul className="flex flex-col gap-3">
                {plan.recipes.map((recipe) => {
                  const on = chosen.has(recipe.id)
                  const own = recipe.custom
                    ? reshapes.find((r) => CUSTOM + r.id === recipe.id)
                    : undefined
                  // Deleted here, still listed until the next check comes back.
                  if (recipe.custom && !own) return null
                  return (
                    <li
                      key={recipe.id}
                      className={cn(
                        'rounded-md border p-3 transition-colors',
                        recipe.blocked
                          ? 'border-hairline bg-surface-1 opacity-70'
                          : on
                            ? 'border-primary bg-surface-2'
                            : 'border-hairline bg-surface-1',
                      )}
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="flex min-w-0 flex-1 flex-col gap-1">
                          {own ? (
                            <div className="flex items-center gap-2">
                              <Checkbox
                                checked={on}
                                disabled={Boolean(recipe.blocked)}
                                onChange={() => toggle(recipe.id)}
                                label={null}
                                aria-label={`Run ${own.name}`}
                              />
                              <Input
                                aria-label="Re-shape name"
                                className="max-w-md flex-1 font-medium"
                                value={own.name}
                                onChange={(e) => updateReshape(own.id, { name: e.target.value })}
                              />
                              <Button
                                size="icon-sm"
                                variant="ghost"
                                aria-label={`Delete ${own.name}`}
                                title="Delete this re-shape"
                                onClick={() => removeReshape(own.id)}
                              >
                                <Trash2Icon />
                              </Button>
                            </div>
                          ) : (
                            <Checkbox
                              checked={on}
                              disabled={Boolean(recipe.blocked)}
                              onChange={() => toggle(recipe.id)}
                              label={<span className="font-medium text-ink">{recipe.name}</span>}
                            />
                          )}
                          {recipe.why && (
                            <p className="max-w-3xl text-body-compact text-pretty text-ink-subtle">
                              {recipe.why}
                            </p>
                          )}
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          {recipe.blocked ? (
                            <span className="text-body-compact text-status-warning">
                              {recipe.blocked}
                            </span>
                          ) : (
                            <span className="num text-caption text-ink-subtle">
                              {fmt.int(recipe.operators)} operators · {fmt.int(recipe.dataFields)}{' '}
                              data fields
                            </span>
                          )}
                          {!own && recipe.transform && (
                            <Button
                              size="sm"
                              variant="ghost"
                              title="Add a copy of this re-shape to edit as your own"
                              onClick={() =>
                                addReshape(`${recipe.name} (edited)`, recipe.transform)
                              }
                            >
                              <PencilIcon />
                              Edit a Copy
                            </Button>
                          )}
                        </div>
                      </div>
                      {own ? (
                        <Textarea
                          aria-label={`${own.name}, written around alpha`}
                          className="mt-2 font-mono"
                          rows={Math.min(8, Math.max(2, own.code.split('\n').length + 1))}
                          spellCheck={false}
                          value={own.code}
                          onChange={(e) => updateReshape(own.id, { code: e.target.value })}
                        />
                      ) : (
                        // Empty for a grouping this market has none of: nothing to show.
                        recipe.transform && (
                          <pre className="num mt-2 overflow-x-auto rounded-sm border border-hairline bg-canvas p-3 text-body-compact whitespace-pre text-ink-muted">
                            {recipe.transform}
                          </pre>
                        )
                      )}
                      {recipe.judged > 0 && (
                        <p
                          className={cn(
                            'mt-2 text-body-compact text-pretty',
                            recipe.worked > 0 ? 'text-ink-muted' : 'text-ink-subtle',
                          )}
                        >
                          Broke Production Correlation for you{' '}
                          <span className="num">
                            {fmt.int(recipe.worked)} of {fmt.int(recipe.judged)}
                          </span>{' '}
                          time{recipe.judged === 1 ? '' : 's'}
                          {recipe.gaveUp ? ', so it starts unticked.' : '.'}
                        </p>
                      )}
                      {recipe.overSingleDataset && !recipe.blocked && (
                        <p className="mt-2 text-body-compact text-pretty text-status-warning">
                          No longer a Single Data Set Alpha: reads {recipe.overSingleDataset}.
                        </p>
                      )}
                      {recipe.overPowerPool && !recipe.blocked && (
                        <p className="mt-2 text-body-compact text-pretty text-status-warning">
                          No longer a Power Pool Alpha: {recipe.overPowerPool}.
                        </p>
                      )}
                      {recipe.caution && (
                        <p className="mt-2 text-body-compact text-pretty text-ink-subtle">
                          <UnlinkIcon className="mr-1 inline size-3.5 align-text-bottom" />
                          {recipe.caution}
                        </p>
                      )}
                    </li>
                  )
                })}
                {/* Added or copied here, listed once the next check comes back. */}
                {reshapes
                  .filter((r) => !plan.recipes.some((row) => row.id === CUSTOM + r.id))
                  .map((r) => (
                    <li key={r.id} className="rounded-md border border-hairline bg-surface-1 p-3">
                      <span className="font-medium text-ink">{r.name}</span>
                      <p className="mt-1 text-body-compact text-ink-subtle">Checking…</p>
                    </li>
                  ))}
              </ul>
            )}
            <div className="mt-3">
              <Button
                variant="secondary"
                onClick={() =>
                  addReshape(`My Re-shape ${reshapes.length + 1}`, 'ts_av_diff(alpha, 60)')
                }
              >
                <PlusIcon />
                Add Re-shape
              </Button>
            </div>
          </Panel>
        </div>
      )}

      {!alphaId && !query.isPending && (
        <Panel>
          <Empty title="Give an Alpha ID">
            One that passes every check but Production Correlation is the one to bring here.
          </Empty>
        </Panel>
      )}
    </Page>
  )
}
