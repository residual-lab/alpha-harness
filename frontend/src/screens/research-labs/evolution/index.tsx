/**
 * Evolution Lab: choose seed Alphas, cores and simulations, then add the breeding to Tasks,
 * where it runs. Children are simulated with an 8-year train and 2-year test split and scored
 * on Fitness over the first 8 years; the last 2 are never used.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { DnaIcon, ListChecksIcon, WandSparklesIcon, XIcon } from 'lucide-react'
import { useEffect, useMemo } from 'react'
import { ApiError } from '@/api/http'
import { CORE_METRICS, DASH, fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { marketKey, regionLabel, useScopeOptions } from '@/lib/scope'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import {
  type EvolutionRequest,
  evolutionLab,
  type SeedReason,
  type SeedRow,
} from '@/screens/research-labs/evolution/api'
import { MAX_SEEDS, useSeedPick } from '@/screens/research-labs/evolution/seed-pick'
import {
  MAX_SIMULATIONS,
  offeredOnly,
  simulationsValid,
  useLabPreview,
} from '@/screens/research-labs/lab-task'
import { NeutralizationPicker } from '@/screens/research-labs/neutralization'
import { CoresSetting, SimulationsSetting } from '@/screens/research-labs/task-settings'
import {
  Button,
  Disclosure,
  Empty,
  ErrorNotice,
  Fieldset,
  figureTone,
  Metric,
  Notice,
  Page,
  PageHeader,
  Panel,
  Progress,
  Segmented,
  TEXT_TONE,
} from '@/ui/kit'
import { Select } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'
import { useEvolutionLab } from './state'

const POPULATIONS = [50, 100, 200]
const MUTATION_RATES = [0.03, 0.05, 0.08]

export function EvolutionLabScreen() {
  const draft = useEvolutionLab()
  const set = useEvolutionLab.setState
  const navigate = useNavigate()

  // Back from Alphas with a finished pick of seeds.
  useEffect(() => {
    const pick = useSeedPick.getState().take('/labs/evolution')
    if (pick)
      set({
        region: pick.scope.region,
        delay: pick.scope.delay,
        universe: pick.scope.universe,
        seedIds: pick.ids,
        autoJobId: null,
      })
  }, [])

  const options = useQuery({
    queryKey: ['evolution-lab', 'options'],
    queryFn: evolutionLab.options,
    staleTime: 5 * 60_000,
  })
  // BRAIN's legal list for the market being bred in; wider than the four the lab defaults to.
  const scopeOptions = useScopeOptions({
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
  })

  const cores = useCores(draft.cores)
  const body: EvolutionRequest = {
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
    alpha_ids: draft.seedIds,
    neutralizations: offeredOnly(draft.neutralizations, scopeOptions.neutralizations),
    cores,
    population: draft.population,
    mutation_rate: draft.mutationRate,
    simulations: draft.simulations ?? 0,
  }
  const { preview, current: planned } = useLabPreview('evolution-lab', body, evolutionLab.preview)
  const plan = preview.data
  const hasSeeds = draft.seedIds.length > 0

  const { autoJobId, appliedJobId } = draft
  const job = useQuery({
    queryKey: ['evolution-lab', 'auto', autoJobId],
    queryFn: () => evolutionLab.autoSeedsJob(autoJobId ?? ''),
    enabled: autoJobId !== null,
    refetchInterval: (query) => (query.state.data?.state === 'running' ? 1000 : false),
  })
  const choosing = job.data?.state === 'running'
  const found = job.data?.state === 'done' ? job.data.result : null
  // Only let the id go when the backend says it does not know the job; any other error may be a
  // timeout over a run that is still going.
  useEffect(() => {
    const error = job.error
    if (error instanceof ApiError && (error.code === 'unknown_job' || error.status === 404))
      set({ autoJobId: null })
  }, [job.error])
  // A finished Auto Select replaces the seeds, once.
  useEffect(() => {
    if (found && autoJobId !== appliedJobId)
      set({
        seedIds: found.seeds.map((s) => s.alphaId),
        appliedJobId: autoJobId,
      })
  }, [found, autoJobId, appliedJobId])

  // The population is sized from the simulations; until they are assigned, seed for the notes' 100.
  const sized = draft.population !== null || draft.simulations !== null
  const wanted = Math.min(
    MAX_SEEDS,
    Math.floor(((sized ? plan?.population : undefined) ?? 100) / 2),
  )
  const auto = useMutation({
    mutationFn: () =>
      evolutionLab.autoSeeds({
        region: draft.region,
        delay: draft.delay,
        universe: draft.universe,
        count: wanted,
      }),
    onSuccess: (r) => set({ autoJobId: r.jobId }),
  })
  const busy = auto.isPending || choosing
  const selectSeeds = () => {
    useSeedPick.getState().start(
      {
        instrumentType: 'EQUITY',
        region: draft.region,
        delay: draft.delay,
        universe: draft.universe,
      },
      draft.seedIds,
      '/labs/evolution',
    )
    void navigate({ to: '/pool/$tab', params: { tab: 'stored' } })
  }

  const current = marketKey(draft)
  const markets = useMemo(() => {
    const items = (options.data?.markets ?? []).map((m) => ({
      value: marketKey(m),
      label: `${regionLabel(m.region)} · D${m.delay} · ${m.universe} (${fmt.int(m.alphas)})`,
    }))
    return items.some((m) => m.value === current)
      ? items
      : [
          {
            value: current,
            label: `${regionLabel(draft.region)} · D${draft.delay} · ${draft.universe}`,
          },
          ...items,
        ]
  }, [options.data, current, draft.region, draft.delay, draft.universe])
  const chooseMarket = (value: string) => {
    if (value === current) return
    const [region, delay, universe] = value.split('|')
    if (region === undefined || universe === undefined) return
    set({
      region,
      delay: Number(delay),
      universe,
      seedIds: [],
      autoJobId: null,
    })
  }

  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const simulations = draft.simulations
  const add = useAddTask(() =>
    evolutionLab.addTask({ ...body, simulations: draft.simulations ?? 0 }),
  )
  const ready =
    plan !== undefined &&
    planned &&
    !choosing &&
    hasSeeds &&
    plan.problems.length === 0 &&
    simulationsValid(simulations, maxSimulations)
  const blocked = !hasSeeds
    ? 'Choose seeds first.'
    : (plan?.problems[0] ?? (simulations === null ? 'Enter the simulations to run.' : null))

  const columns: Column<SeedRow>[] = [
    {
      key: 'alpha',
      header: 'Alpha',
      width: '96px',
      cell: (r) => <span className="num text-ink-muted">{r.alphaId}</span>,
    },
    {
      key: 'expression',
      header: 'Expression',
      width: 'minmax(260px,3fr)',
      cell: (r) => (
        <span className="num truncate" title={r.expression}>
          {r.expression}
        </span>
      ),
    },
    {
      key: 'sharpe',
      header: 'Sharpe',
      width: '80px',
      align: 'right',
      cell: (r) => (
        <span className={TEXT_TONE[figureTone('sharpe', r.sharpe, r.limits)]}>
          {fmt.ratio(r.sharpe)}
        </span>
      ),
    },
    {
      key: 'fitness',
      header: 'Fitness',
      width: '80px',
      align: 'right',
      cell: (r) => fmt.ratio(r.fitness),
    },
    {
      key: 'turnover',
      header: 'Turnover',
      width: '88px',
      align: 'right',
      cell: (r) => CORE_METRICS.turnover.show(r.turnover),
    },
    {
      key: 'remove',
      header: '',
      width: '44px',
      align: 'right',
      cell: (r) => (
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={`Remove ${r.alphaId}`}
          title="Remove"
          onClick={() => set({ seedIds: draft.seedIds.filter((id) => id !== r.alphaId) })}
        >
          <XIcon />
        </Button>
      ),
    },
  ]

  return (
    <Page>
      <PageHeader
        sticky
        title="Evolution Lab"
        actions={<AddTaskButtons add={add} disabled={!ready} blocked={blocked} />}
      />
      {options.isError && (
        <ErrorNotice error={options.error} title="Could not load the lab's options" />
      )}
      <Panel
        title="Seeds"
        bodyClassName="flex flex-col gap-3"
        actions={
          <>
            <Select
              label="Market"
              mono
              className="h-7 text-body-compact"
              items={markets}
              value={current}
              onChange={chooseMarket}
            />
            {hasSeeds && (
              <>
                <Button size="sm" onClick={selectSeeds}>
                  <ListChecksIcon />
                  Select Seeds
                </Button>
                <Button size="sm" loading={busy} onClick={() => auto.mutate()}>
                  {!busy && <WandSparklesIcon />}
                  Auto Select
                </Button>
              </>
            )}
          </>
        }
      >
        {choosing && (
          <div className="flex flex-col gap-1.5">
            <Progress value={job.data?.progress ?? null} label="Choosing seeds" />
            <span className="text-body-compact text-pretty text-ink-subtle">
              {job.data?.detail || 'Choosing seeds'}
            </span>
          </div>
        )}
        {job.data?.state === 'failed' && (
          <Notice tone="error" title="Auto Select stopped">
            {job.data.error}
          </Notice>
        )}
        {job.isError && (
          <ErrorNotice error={job.error} title="Could not read Auto Select's progress" />
        )}
        {hasSeeds ? (
          <DataTable
            label="Seeds"
            rows={plan?.seeds ?? []}
            columns={columns}
            rowKey={(r) => r.alphaId}
            loading={preview.isPending}
            error={preview.error}
            maxHeight="40vh"
            empty="None of these Alphas can be seeds."
          />
        ) : (
          !choosing && (
            <Empty title="No seeds chosen" icon={<DnaIcon />}>
              <div className="mt-2 flex flex-wrap justify-center gap-2">
                <Button variant="primary" onClick={selectSeeds}>
                  <ListChecksIcon />
                  Select Seeds
                </Button>
                <Button loading={busy} onClick={() => auto.mutate()}>
                  {!busy && <WandSparklesIcon />}
                  Auto Select
                </Button>
              </div>
            </Empty>
          )
        )}
        {found && found.seeds.length < found.wanted && (
          <Notice
            tone="warn"
            title={`${fmt.int(found.seeds.length)} of ${fmt.int(found.wanted)} seeds found in ${fmt.int(found.examined)} Alphas examined.`}
          />
        )}
        {found && found.reasons.length > 0 && (
          <Disclosure summary={`Not Chosen (${fmt.int(found.reasons.length)})`}>
            <Reasons items={found.reasons} />
          </Disclosure>
        )}
        {hasSeeds && plan && plan.skipped.length > 0 && (
          <Notice tone="warn" title={`${fmt.int(plan.skipped.length)} of these can't be seeds`}>
            <Reasons items={plan.skipped} />
          </Notice>
        )}
      </Panel>

      <Panel title="Settings">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
            <CoresSetting value={cores} onChange={(next) => set({ cores: next })} />
            <SimulationsSetting
              value={simulations}
              max={maxSimulations}
              placeholder="5000"
              onChange={(next) => set({ simulations: next })}
            />
          </div>
          {scopeOptions.neutralizations.length > 0 && (
            <NeutralizationPicker
              available={scopeOptions.neutralizations}
              value={draft.neutralizations}
              onChange={(next) => set({ neutralizations: next })}
            />
          )}
          <Disclosure summary="Advanced">
            <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
              <Fieldset legend="Population">
                <Segmented
                  label="Population"
                  items={[
                    { value: 0, label: 'Auto' },
                    ...(options.data?.populations ?? POPULATIONS).map((v) => ({
                      value: v,
                      label: v,
                    })),
                  ]}
                  value={draft.population ?? 0}
                  onChange={(v) => set({ population: v === 0 ? null : v })}
                />
              </Fieldset>
              <Fieldset legend="Mutation">
                <Segmented
                  label="Mutation"
                  items={(options.data?.mutationRates ?? MUTATION_RATES).map((v) => ({
                    value: v,
                    label: fmt.pct(v, 0),
                  }))}
                  value={draft.mutationRate}
                  onChange={(mutationRate) => set({ mutationRate })}
                />
              </Fieldset>
            </div>
          </Disclosure>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric boxed label="Seeds" value={fmt.int(hasSeeds ? plan?.seeds.length : 0)} />
            <Metric boxed label="Population" value={sized ? fmt.int(plan?.population) : DASH} />
            <Metric
              boxed
              label="Generations"
              value={plan?.generations ? fmt.int(plan.generations) : DASH}
            />
            <Metric
              boxed
              label="Market"
              value={`${draft.region} · D${draft.delay}`}
              hint={draft.universe}
            />
          </div>
          <p className="text-body-compact text-pretty text-ink-subtle">
            Scored on Fitness over the first 8 years; the last 2 stay untouched.
          </p>
          {preview.isError && <ErrorNotice error={preview.error} title="Could not plan the task" />}
          {plan?.problems.map((m) => (
            <Notice key={m} tone="error" title={m} />
          ))}
          {plan && plan.sample.length > 0 && (
            <Disclosure summary="Sample Alphas">
              <ul className="flex flex-col gap-2">
                {plan.sample.map((s, i) => (
                  <li
                    key={i}
                    className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5"
                  >
                    <code className="num text-body-compact break-all text-ink">{s.expression}</code>
                    <span className="text-body-compact text-ink-subtle">
                      {String(s.settings['neutralization'] ?? DASH)} · Decay{' '}
                      <span className="num">{String(s.settings['decay'] ?? DASH)}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </Disclosure>
          )}
        </div>
      </Panel>
    </Page>
  )
}

function Reasons({ items }: { items: SeedReason[] }) {
  return (
    <ul className="flex max-h-64 flex-col gap-1 overflow-y-auto">
      {items.map((r) => (
        <li key={r.alphaId} className="flex gap-3 text-body-compact">
          <span className="num w-20 shrink-0 text-ink-muted">{r.alphaId}</span>
          <span className="min-w-0 break-words text-ink-subtle">{r.reason}</span>
        </li>
      ))}
    </ul>
  )
}
