/**
 * Search Lab: choose datasets, cores and simulations, then run the search now or add it to
 * Tasks. It writes one- and two-operator Alphas from the datasets' fields, steering towards
 * the best Sharpe.
 */

import { useQuery } from '@tanstack/react-query'
import { today } from '@/api/core'
import { useCores } from '@/lib/preferences'
import { useScopeOptions } from '@/lib/scope'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import {
  labBody,
  MAX_SIMULATIONS,
  simulationsValid,
  useLabMarket,
  useLabPreview,
  vectorOperatorsOf,
} from '@/screens/research-labs/lab-task'
import { type SearchLabRequest, searchLab } from '@/screens/research-labs/search/api'
import { useSearchLab } from '@/screens/research-labs/search/state'
import { DatasetsPanel, MarketPanel, SettingsPanel } from '@/screens/research-labs/task-settings'
import { ErrorNotice, Page, PageHeader } from '@/ui/kit'

export function SearchLabScreen() {
  const stored = useSearchLab()
  const set = useSearchLab.setState
  const day = useQuery({ queryKey: ['today'], queryFn: () => today.get() })
  const { chosen, scope, choose, move } = useLabMarket(stored, set, '/labs/search')

  const options = useQuery({
    queryKey: ['search-lab', 'options'],
    queryFn: searchLab.options,
    staleTime: 5 * 60_000,
  })
  // Until the user types a number, the task takes what is left of today (never stored).
  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const unspoken = day.data?.simulations.unspoken ?? 0
  const suggested = unspoken > 0 ? Math.min(unspoken, maxSimulations) : null
  const draft = { ...stored, simulations: stored.simulations ?? suggested }
  const vectorOperators = vectorOperatorsOf(draft, options.data?.vector)
  const cores = useCores(draft.cores)
  const { neutralizations } = useScopeOptions({
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
  })
  const body: SearchLabRequest = labBody(draft, vectorOperators, cores, neutralizations)
  const { preview, current } = useLabPreview('search-lab', body, searchLab.preview, {
    enabled: chosen && options.isSuccess,
  })
  const plan = chosen ? preview.data : undefined

  const add = useAddTask(() => searchLab.addTask({ ...body, simulations: draft.simulations ?? 0 }))
  const ready =
    plan !== undefined &&
    current &&
    plan.problems.length === 0 &&
    simulationsValid(draft.simulations, maxSimulations)
  // What stops both buttons that no panel below already says.
  const blocked = !chosen
    ? 'Choose datasets first.'
    : draft.simulations === null
      ? 'Enter the simulations to run.'
      : null

  return (
    <Page>
      <PageHeader
        sticky
        title="Search Lab"
        actions={<AddTaskButtons add={add} disabled={!ready} blocked={blocked} />}
      />
      {options.isError && (
        <ErrorNotice error={options.error} title="Could not read your operators" />
      )}
      <MarketPanel scope={scope} onChange={move} />
      <DatasetsPanel
        ids={draft.datasetIds}
        scope={scope}
        onApply={(datasetIds, fieldFilter) => set({ datasetIds, fieldFilter })}
        onExplore={choose}
        filter={draft.fieldFilter}
        onClearFilter={() => set({ fieldFilter: null })}
        onRemove={(ids) => set({ datasetIds: stored.datasetIds.filter((x) => !ids.includes(x)) })}
      />
      <SettingsPanel
        // The box holds only what was typed, so it can be emptied; the suggestion shows faintly.
        draft={stored}
        suggested={suggested}
        set={set}
        vector={options.data?.vector ?? []}
        chosenVector={vectorOperators}
        decays={options.data?.decays}
        maxSimulations={maxSimulations}
        plan={plan}
        error={chosen && preview.isError ? preview.error : null}
      />
    </Page>
  )
}
