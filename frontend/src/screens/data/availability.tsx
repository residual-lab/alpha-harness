/** The availability filters: whether a field also exists in other regions. */

import { useQuery } from '@tanstack/react-query'
import { GlobeIcon, MapPinIcon } from 'lucide-react'
import { type ReactNode, useEffect } from 'react'
import { type CatalogFacets, catalog } from '@/api/catalog'
import { REGION_AGNOSTIC_REGION } from '@/api/platform'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { isRegionAgnostic, regionLabel, runsRegionAgnostic } from '@/lib/scope'
import { Button } from '@/ui/kit'
import { type FieldFilterState, useFieldFilter } from './state'

type Counts = CatalogFacets['availability']

function Toggle({
  on,
  disabled,
  icon,
  label,
  count,
  title,
  onClick,
}: {
  on: boolean
  disabled?: boolean
  icon: ReactNode
  label: string
  count: number | undefined
  title: string
  onClick: () => void
}) {
  return (
    <Button
      variant={on ? 'primary' : 'secondary'}
      size="sm"
      aria-pressed={on}
      disabled={disabled}
      title={title}
      onClick={onClick}
    >
      {icon}
      {label}
      <span className={cn('num', on ? 'opacity-80' : 'text-ink-subtle')}>{fmt.int(count)}</span>
    </Button>
  )
}

export function AvailabilityFilters({
  scope,
  counts,
  value,
  onChange,
}: {
  scope: Scope
  counts: Counts | undefined
  /** A filter of the caller's own, e.g. one Template Lab variable's; the Data Explorer's by default. */
  value?: FieldFilterState | undefined
  onChange?: ((change: Partial<FieldFilterState>) => void) | undefined
}) {
  const explorer = useFieldFilter((s) => s.filter)
  const setExplorer = useFieldFilter((s) => s.set)
  const filter = value ?? explorer
  const set = onChange ?? setExplorer
  const scopes = useQuery({ queryKey: ['catalog', 'scopes'], queryFn: catalog.scopes })

  const offersAgnostic = runsRegionAgnostic(scope)
  const offersRegion = !isRegionAgnostic(scope)
  // Region ALL arrives by its own download; without it no field can be found there.
  const allDownloaded = scopes.data
    ? scopes.data.some(
        (s) => s.region === REGION_AGNOSTIC_REGION && s.instrument_type === scope.instrumentType,
      )
    : true

  // A toggle this market does not offer would filter the table while no button shows it.
  useEffect(() => {
    const drop: Record<string, false> = {}
    if (filter.region_agnostic && !offersAgnostic) drop['region_agnostic'] = false
    if (filter.region_exclusive && !offersRegion) drop['region_exclusive'] = false
    if (Object.keys(drop).length) set(drop)
  }, [filter.region_agnostic, filter.region_exclusive, offersAgnostic, offersRegion, set])

  if (!offersAgnostic && !offersRegion) return null
  const region = regionLabel(scope.region)
  return (
    <div role="group" aria-label="Availability" className="flex flex-wrap items-center gap-2">
      <span className="text-body-compact text-ink-subtle">Availability</span>
      {offersAgnostic && (
        <Toggle
          on={Boolean(filter.region_agnostic)}
          // Pressed already, it stays releasable: a disabled button could not be turned off.
          disabled={!allDownloaded && !filter.region_agnostic}
          icon={<GlobeIcon />}
          label="Region Agnostic"
          count={allDownloaded ? counts?.region_agnostic : undefined}
          title={
            allDownloaded
              ? 'Only Fields that also exist in the All Regions market, so a region-agnostic simulation can use them'
              : 'Download the All Regions market in BRAIN › Sync first'
          }
          onClick={() => set({ region_agnostic: !filter.region_agnostic, region_exclusive: false })}
        />
      )}
      {offersRegion && (
        <Toggle
          on={Boolean(filter.region_exclusive)}
          icon={<MapPinIcon />}
          label={`${region} Exclusive`}
          count={counts?.region_exclusive}
          title={`Only Fields found in ${region} and in no other downloaded region, All Regions included`}
          onClick={() =>
            set({ region_exclusive: !filter.region_exclusive, region_agnostic: false })
          }
        />
      )}
    </div>
  )
}
