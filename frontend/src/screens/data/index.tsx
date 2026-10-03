/**
 * Data Explorer (spec §4.2): a synced market's fields, narrowed in More Filters down to whole
 * categories, whole subcategories or single datasets. Syncing lives in BRAIN › Sync.
 */

import { useNavigate } from '@tanstack/react-router'
import { useEffect } from 'react'
import type { Scope } from '@/api/types'
import { fmt } from '@/lib/format'
import { useScope } from '@/lib/scope'
import { useDatasetPick } from '@/screens/data/dataset-pick'
import { TAKES_FIELDS, useHandOver, useSelectionSplit, useTicked } from '@/screens/data/field-pick'
import { Button, Metric, Page, PageHeader } from '@/ui/kit'
import { FieldsTab } from './fields'
import { MarketBar } from './market'

export function DataScreen() {
  const [scope, update] = useScope('data')
  const picking = useDatasetPick((s) => s.active)

  // A pick follows the market shown here.
  useEffect(() => {
    if (picking) useDatasetPick.getState().follow(scope)
  }, [picking, scope])

  return (
    <Page>
      <PageHeader
        title="Data Explorer"
        description="Browse, filter and compare the Data Fields you synced from BRAIN, locally."
      />
      {picking && <PickBar scope={scope} />}
      <MarketBar scope={scope} update={update} />
      <FieldsTab scope={scope} />
    </Page>
  )
}

/**
 * While a lab picks: what is selected, where to select it, and the way back. Selected fields win
 * over ticked datasets: the lab gets only them, ranked by the table's sort.
 */
function PickBar({ scope }: { scope: Scope }) {
  const navigate = useNavigate()
  const datasets = useDatasetPick((s) => s.ids.length)
  // The typed template editor takes datasets under a filter, not single fields.
  const takesFields = useDatasetPick((s) => TAKES_FIELDS.has(s.from))
  const ticked = useTicked(scope).length
  const split = useSelectionSplit(scope)
  // Until the split is in, every tick counts as selected.
  const fields = !takesFields ? 0 : split.loading ? ticked : split.selected.length
  const excluded = takesFields ? ticked - fields : 0
  const handOver = useHandOver()
  const cancel = () => {
    const pick = useDatasetPick.getState()
    pick.cancel()
    void navigate({ to: pick.from })
  }

  return (
    <div className="sticky top-0 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-hairline bg-surface-1 p-3">
      {takesFields && <Metric boxed size="sm" label="Fields Selected" value={fmt.int(fields)} />}
      {excluded > 0 && (
        <Metric boxed size="sm" label="Fields Excluded" value={fmt.int(excluded)} tone="warn" />
      )}
      <Metric
        boxed
        size="sm"
        label="Datasets Ticked"
        value={fmt.int(datasets)}
        hint={
          takesFields && fields > 0 && datasets > 0
            ? 'Only filtering: the fields go instead'
            : undefined
        }
      />
      <span className="min-w-0 flex-1 text-body-compact text-pretty text-ink-subtle">
        {takesFields
          ? "Tick single fields in the table to give the lab only those, ranked by the table's sort. Or tick whole categories, subcategories or datasets in More Filters to give it every field your filters show."
          : 'Tick whole categories, subcategories or datasets in More Filters. The template uses only the fields your filters show.'}
      </span>
      <Button variant="ghost" onClick={cancel}>
        Cancel
      </Button>
      <Button
        variant="primary"
        disabled={takesFields && ticked > 0 ? fields === 0 : datasets === 0}
        loading={handOver.isPending}
        onClick={() => handOver.mutate({ scope, to: useDatasetPick.getState().from, finish: true })}
      >
        Done
      </Button>
    </div>
  )
}
