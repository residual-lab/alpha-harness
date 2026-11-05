/**
 * Data Explorer (spec §4.2): a synced market's fields, narrowed in More Filters down to whole
 * categories, whole subcategories or single datasets. Syncing lives in BRAIN › Sync.
 */

import { useEffect } from 'react'
import { useScope } from '@/lib/scope'
import { useDatasetPick } from '@/screens/data/dataset-pick'
import { Page, PageHeader } from '@/ui/kit'
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
      <MarketBar scope={scope} update={update} />
      <FieldsTab scope={scope} />
    </Page>
  )
}
