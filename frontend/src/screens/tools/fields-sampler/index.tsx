/**
 * Fields Sampler: one Alpha re-run with each of its data fields swapped for others, one at a
 * time, its settings untouched. Every swap nothing refuses is swept the same way, until the
 * task has run the simulations it was given.
 */

import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { useEffect, useMemo, useState } from 'react'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { SETTINGS } from '@/screens/alpha/settings-list'
import { BRAIN_ALPHA_URL } from '@/screens/pool/api'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import { MAX_SIMULATIONS } from '@/screens/research-labs/lab-task'
import { CoresSetting, SimulationsSetting } from '@/screens/research-labs/task-settings'
import { templateLab } from '@/screens/research-labs/template/api'
import { TemplateEditor } from '@/screens/research-labs/template/editor'
import { AlphaPanel } from '@/screens/tools/one-alpha'
import { Empty, Metric, Page, PageHeader, Panel, Segmented, Skeleton } from '@/ui/kit'
import { Tile } from '@/ui/tile'
import { type FieldSlot, fieldsSampler, type ProvenSettings, type SamplerMode } from './api'

const MODES: { value: SamplerMode; label: string }[] = [
  { value: 'sweep', label: 'Sweep' },
  { value: 'similar', label: 'Similar' },
  { value: 'buckets', label: 'Buckets' },
]

const ABOUT: Record<SamplerMode, string> = {
  sweep:
    'Every other field of the same dataset, best covered first. Keeps a single dataset Alpha single dataset.',
  similar:
    'The fields whose descriptions read closest to it: the closest few from its own dataset, then its subcategory, its category, and the whole market.',
  buckets:
    'Every field of the dataset in the bucket of the field it is most like, by what tells them apart. Every field changes at once, one from each bucket’s top picks.',
}

const PER_TIER = [5, 10, 20]
const TOP_PICKS = [5, 10, 15, 20, 30]

const TIER: Record<string, string> = {
  dataset: 'Same dataset',
  subcategory: 'Same subcategory',
  category: 'Same category',
  market: 'Anywhere in the market',
}

/** A plain expression, shown and not edited: the editor's other inputs stay empty. */
const NO_PRESETS: Record<string, string> = {}
const NO_VARIABLES = {}
const NO_INFOS = new Map()
const NO_PROBLEMS: string[] = []
const IGNORE = () => {}

/** Two rows of five: Instrument Type, Language and Unit Handling left out. */
const HELD = SETTINGS.filter(
  ([key]) => !['instrumentType', 'language', 'unitHandling'].includes(key),
)

/** Candidates shown before the rest are summed up. */
const SHOWN = 24

export function FieldsSamplerScreen() {
  const search = useSearch({ from: '/tools/fields-sampler' })
  const navigate = useNavigate()
  // The URL owns which Alpha is open, so a link from the Alpha screen is shareable.
  const alphaId = search.alpha ?? ''
  const [draft, setDraft] = useState(alphaId)
  /** `null` until chosen here: Buckets for an Alpha it fits, Sweep for the rest. */
  const [chosenMode, setMode] = useState<SamplerMode | null>(null)
  const [k, setK] = useState(10)
  const [chosen, setChosen] = useState<ReadonlySet<string>>(new Set())
  const [included, setIncluded] = useState<ReadonlySet<string>>(new Set())
  const [budget, setBudget] = useState<number | null>(null)
  /** `null` until chosen here: until then Settings' default for new tasks applies. */
  const [chosenCores, setCores] = useState<number | null>(null)
  const cores = useCores(chosenCores)

  useEffect(() => {
    setDraft(search.alpha ?? '')
    setMode(null)
  }, [search.alpha])

  const sweep = useQuery({
    queryKey: ['fields-sampler', alphaId, 'sweep', 0],
    queryFn: () =>
      fieldsSampler.preview({
        alphaId,
        mode: 'sweep',
        k,
        fields: [],
        alphas: [],
        budget: null,
        cores: 1,
      }),
    enabled: alphaId.length > 0,
    retry: false,
  })
  const mode = chosenMode ?? (sweep.data?.bucketable ? 'buckets' : 'sweep')
  const query = useQuery({
    // Only Similar's list depends on k: a bucket is in pick order, and k only cuts it.
    queryKey: ['fields-sampler', alphaId, mode, mode === 'similar' ? k : 0],
    queryFn: () =>
      fieldsSampler.preview({ alphaId, mode, k, fields: [], alphas: [], budget: null, cores: 1 }),
    enabled: alphaId.length > 0 && (mode !== 'sweep' || sweep.isFetched),
    retry: false,
  })
  const plan = query.data
  // The operators the editor colours and explains on hover; the Template Lab's own query.
  const operators = useQuery({
    queryKey: ['template-lab', 'options'],
    queryFn: () => templateLab.options(),
    staleTime: 5 * 60_000,
  })
  const reference = useMemo(() => operators.data?.reference ?? [], [operators.data])
  const swappable = useMemo(
    () => (plan?.slots ?? []).filter((s) => s.candidates.length > 0).map((s) => s.field),
    [plan],
  )

  // A new Alpha, or a new way of picking, starts with every field that has a swap.
  useEffect(() => setChosen(new Set(swappable)), [swappable])
  // ...and with every other setting the expression already works at: that is the point of them.
  const others = useMemo(() => plan?.others ?? [], [plan])
  useEffect(() => setIncluded(new Set(others.map((o) => o.alphaId))), [others])

  const bucketed = mode === 'buckets'
  // A swap to a field the expression already reads is skipped, as the server skips it.
  const own = new Set(plan?.slots.map((s) => s.field))
  // Buckets runs every pick of one bucket with every pick of the others.
  const grid = (counts: number[]) => counts.reduce((n, c) => n * Math.min(k, c), 1)
  const first = bucketed
    ? grid((plan?.slots ?? []).map((s) => s.candidates.length))
    : (plan?.slots ?? [])
        .filter((s) => chosen.has(s.field))
        .reduce((sum, s) => sum + s.candidates.filter((c) => !own.has(c.field)).length, 0)
  const elsewhere = others
    .filter((o) => included.has(o.alphaId))
    .reduce(
      (sum, o) =>
        sum +
        (bucketed
          ? grid(o.swaps.map((w) => w.count))
          : o.swaps.filter((w) => chosen.has(w.field)).reduce((n, w) => n + w.count, 0)),
      0,
    )
  const total = first + elsewhere
  const overMax = budget !== null && budget > MAX_SIMULATIONS

  const analyse = (event: React.FormEvent) => {
    event.preventDefault()
    void navigate({
      to: '/tools/fields-sampler',
      search: { alpha: draft.trim() || undefined },
      replace: true,
    })
  }

  const add = useAddTask(() =>
    fieldsSampler.addTask({
      alphaId,
      mode,
      k,
      fields: [...chosen],
      alphas: [...included],
      budget,
      cores,
    }),
  )
  // A typed ID that has not been analysed is not the Alpha the fields below were read from.
  const analysed = draft.trim() === alphaId

  const toggle = (field: string) =>
    setChosen((prev) => {
      const next = new Set(prev)
      if (next.has(field)) next.delete(field)
      else next.add(field)
      return next
    })

  return (
    <Page>
      <PageHeader
        sticky
        title="Fields Sampler"
        actions={
          <AddTaskButtons
            add={add}
            disabled={(!bucketed && chosen.size === 0) || !analysed || first === 0 || overMax}
            blocked={
              !analysed
                ? 'Analyse an Alpha first.'
                : !bucketed && chosen.size === 0
                  ? 'Tick the fields to swap.'
                  : null
            }
          />
        }
      />

      <AlphaPanel
        draft={draft}
        setDraft={setDraft}
        onAnalyse={analyse}
        error={query.isError ? query.error : sweep.isError ? sweep.error : null}
        problems={plan?.problems}
      />

      {alphaId && (query.isPending || sweep.isPending) && (
        <Skeleton className="h-64" label="Reading the Alpha" />
      )}

      {plan?.expression && (
        // Read for the Alpha last analysed: faded once another ID is typed, until it is analysed.
        <div
          className={cn(
            'flex flex-col gap-4 transition-opacity',
            !analysed && 'pointer-events-none opacity-50',
          )}
        >
          <Panel title="Alpha" bodyClassName="flex flex-col gap-4">
            <TemplateEditor
              readOnly
              label="Alpha Expression"
              value={plan.expression}
              onChange={IGNORE}
              reference={reference}
              presets={NO_PRESETS}
              variables={NO_VARIABLES}
              infos={NO_INFOS}
              problems={NO_PROBLEMS}
            />
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
              {HELD.map(([key, title]) => (
                <Metric
                  key={key}
                  boxed
                  size="sm"
                  label={title}
                  value={plan.settingLabels[key] ?? String(plan.rawSettings[key] ?? '')}
                />
              ))}
            </div>
          </Panel>

          {others.length > 0 && (
            <Panel
              title="Also SUBMITTED in"
              description="The same expression is submitted at these settings too. Every swap also runs at each one you keep, all in this one task."
              actions={
                <span className="num text-body-compact whitespace-nowrap text-ink-muted">
                  {fmt.int(total)} simulations · {fmt.int(1 + included.size)} settings
                </span>
              }
            >
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                {others.map((other) => (
                  <ProvenCard
                    key={other.alphaId}
                    other={other}
                    on={included.has(other.alphaId)}
                    onToggle={() =>
                      setIncluded((prev) => {
                        const next = new Set(prev)
                        if (next.has(other.alphaId)) next.delete(other.alphaId)
                        else next.add(other.alphaId)
                        return next
                      })
                    }
                  />
                ))}
              </div>
            </Panel>
          )}

          <Panel
            title="Swap For"
            description={ABOUT[mode]}
            actions={
              <span className="flex flex-wrap items-center gap-3">
                <Segmented
                  label="How to pick the swaps"
                  items={MODES.filter((m) => m.value !== 'buckets' || plan.bucketable)}
                  value={mode}
                  onChange={setMode}
                />
                {mode === 'similar' && (
                  <Segmented
                    label="Fields from each tier"
                    items={PER_TIER.map((n) => ({ value: n, label: `${n} a tier` }))}
                    value={k}
                    onChange={setK}
                  />
                )}
                {bucketed && (
                  <Segmented
                    label="Top picks from each bucket"
                    items={TOP_PICKS.map((n) => ({ value: n, label: `Top ${n}` }))}
                    value={k}
                    onChange={setK}
                  />
                )}
              </span>
            }
          >
            <div className="flex flex-col gap-3">
              {plan.slots.map((slot) =>
                bucketed ? (
                  <BucketTile key={slot.field} slot={slot} top={k} />
                ) : (
                  <SlotTile
                    key={slot.field}
                    slot={slot}
                    on={chosen.has(slot.field)}
                    onToggle={() => toggle(slot.field)}
                  />
                ),
              )}
            </div>
          </Panel>

          <Panel title="Task">
            <div className="flex flex-wrap items-end gap-4">
              <SimulationsSetting
                value={budget}
                max={MAX_SIMULATIONS}
                placeholder="Unlimited"
                onChange={setBudget}
              />
              <CoresSetting value={cores} onChange={setCores} />
            </div>
            {budget !== null && budget < total && (
              <p className="mt-3 text-body-compact text-ink-subtle">
                {bucketed
                  ? 'Fewer than the grid: the top picks of every bucket run together first.'
                  : 'Fewer than the first sweep: its best-covered swaps run, every field taking turns, and nothing is left to grow from.'}
              </p>
            )}
          </Panel>
        </div>
      )}

      {!alphaId && !query.isPending && (
        <Panel>
          <Empty title="Give an Alpha ID">
            Each of its data fields is swapped for others, one at a time; its settings stay as they
            are.
          </Empty>
        </Panel>
      )}
    </Page>
  )
}

function ProvenCard({
  other,
  on,
  onToggle,
}: {
  other: ProvenSettings
  on: boolean
  onToggle: () => void
}) {
  const s = other.settings
  return (
    <button
      type="button"
      aria-pressed={on}
      title={`${other.alphaId} · Ctrl-click opens it on BRAIN`}
      onClick={(e) => {
        if (e.ctrlKey || e.metaKey) {
          window.open(BRAIN_ALPHA_URL(other.alphaId), '_blank', 'noopener,noreferrer')
        } else onToggle()
      }}
      className={cn(
        'num rounded-md border p-3 text-left text-body font-medium transition-colors',
        on
          ? 'border-primary bg-primary-subtle text-ink'
          : 'border-(--field-border) bg-surface-1 text-ink-muted hover:border-(--field-border-hover)',
      )}
    >
      {s.region} · D{s.delay} · {s.universe}
    </button>
  )
}

function SlotTile({ slot, on, onToggle }: { slot: FieldSlot; on: boolean; onToggle: () => void }) {
  // In the server's order, nearest tier first.
  const tiers = Object.keys(TIER)
    .map((tier) => [tier, slot.candidates.filter((c) => c.tier === tier)] as const)
    .filter(([, found]) => found.length > 0)
  const none = slot.candidates.length === 0
  return (
    <Tile
      label={slot.field}
      any={on && !none}
      {...(none ? {} : { onToggle })}
      actions={
        <span className="num text-body-compact whitespace-nowrap text-ink-subtle">
          {fmt.int(slot.candidates.length)} swap{slot.candidates.length === 1 ? '' : 's'}
        </span>
      }
    >
      {none ? (
        <p className="text-body-compact text-pretty text-ink-subtle">{slot.problem}</p>
      ) : (
        tiers.map(([tier, found]) => (
          <div key={tier} className="flex flex-col gap-1.5">
            {tiers.length > 1 && (
              <span className="text-caption font-medium text-ink-subtle">{TIER[tier] ?? tier}</span>
            )}
            <p className="num flex flex-wrap gap-x-3 gap-y-1 text-body-compact text-ink-muted">
              {found.slice(0, SHOWN).map((c) => (
                <span key={c.field} title={`${c.dataset} · coverage ${fmt.pct(c.coverage, 0)}`}>
                  {c.field}
                </span>
              ))}
              {found.length > SHOWN && (
                <span className="text-ink-subtle">and {fmt.int(found.length - SHOWN)} more</span>
              )}
            </p>
          </div>
        ))
      )}
    </Tile>
  )
}

/** One field's bucket: its top picks, which the grid runs, then how many more it holds. */
function BucketTile({ slot, top }: { slot: FieldSlot; top: number }) {
  const picks = slot.candidates.slice(0, top)
  return (
    <Tile
      label={slot.field}
      any
      actions={
        <span className="num text-body-compact whitespace-nowrap text-ink-subtle">
          {fmt.int(picks.length)} of {fmt.int(slot.candidates.length)} in its bucket
        </span>
      }
    >
      {picks.length === 0 ? (
        <p className="text-body-compact text-pretty text-ink-subtle">{slot.problem}</p>
      ) : (
        <p className="num flex flex-wrap gap-x-3 gap-y-1 text-body-compact text-ink-muted">
          {picks.map((c) => (
            <span key={c.field} title={`coverage ${fmt.pct(c.coverage, 0)}`}>
              {c.field}
            </span>
          ))}
        </p>
      )}
    </Tile>
  )
}
