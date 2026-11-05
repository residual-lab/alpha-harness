/**
 * Correlation Breaker (Tools): an Alpha BRAIN says is already in the production pool, re-shaped.
 *
 * The settings are the Alpha's own and are shown as read-only, because the whole idea is to
 * hold everything it was judged on and change only what it is exposed to. Every re-shape is
 * printed in full before anything is queued: a consultant should be able to read what will run.
 */

import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useSearch } from '@tanstack/react-router'
import { UnlinkIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { PROD_CORRELATION_CEILING } from '@/api/platform'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import { CoresSetting } from '@/screens/research-labs/task-settings'
import { AlphaPanel, HeldMetrics } from '@/screens/tools/one-alpha'
import { Checkbox, Empty, LINK, Notice, Page, PageHeader, Panel, Skeleton } from '@/ui/kit'
import { correlationBreaker } from './api'

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

  const query = useQuery({
    queryKey: ['correlation-breaker', alphaId],
    queryFn: () => correlationBreaker.preview({ alphaId, recipes: [], cores: 1 }),
    enabled: alphaId.length > 0,
    retry: false,
  })
  const plan = query.data
  const runnable = useMemo(() => (plan?.recipes ?? []).filter((r) => !r.blocked), [plan])

  // A new Alpha starts with every re-shape it can run ticked: the point is to try them. One
  // that would cost a Power Pool Alpha its eligibility waits to be asked for.
  useEffect(() => {
    setChosen(new Set(runnable.filter((r) => !r.overPowerPool).map((r) => r.id)))
  }, [runnable])

  const analyse = (event: React.FormEvent) => {
    event.preventDefault()
    void navigate({
      to: '/tools/correlation-breaker',
      search: { alpha: draft.trim() || undefined },
      replace: true,
    })
  }

  const add = useAddTask(() => correlationBreaker.addTask({ alphaId, recipes: [...chosen], cores }))
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
            disabled={chosen.size === 0 || !analysed}
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
            description="Each one wraps the Alpha below. Only the re-shape is shown; the binding is the same for all of them."
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
                        <div className="flex min-w-0 flex-col gap-1">
                          <Checkbox
                            checked={on}
                            disabled={Boolean(recipe.blocked)}
                            onChange={() => toggle(recipe.id)}
                            label={<span className="font-medium text-ink">{recipe.name}</span>}
                          />
                          {recipe.why && (
                            <p className="max-w-3xl text-body-compact text-pretty text-ink-subtle">
                              {recipe.why}
                            </p>
                          )}
                        </div>
                        {recipe.blocked ? (
                          <span className="shrink-0 text-body-compact text-status-warning">
                            {recipe.blocked}
                          </span>
                        ) : (
                          <span className="num shrink-0 text-caption text-ink-subtle">
                            {fmt.int(recipe.operators)} operators · {fmt.int(recipe.dataFields)}{' '}
                            data fields
                          </span>
                        )}
                      </div>
                      {/* Empty for a grouping this market has none of: nothing to show. */}
                      {recipe.transform && (
                        <pre className="num mt-2 overflow-x-auto rounded-sm border border-hairline bg-canvas p-3 text-body-compact whitespace-pre text-ink-muted">
                          {recipe.transform}
                        </pre>
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
              </ul>
            )}
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
