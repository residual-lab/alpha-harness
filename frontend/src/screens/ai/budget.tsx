import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { today } from '@/api/core'
import { DAILY_SIMULATIONS } from '@/api/platform'
import { fmt } from '@/lib/format'
import type { LLMKey, LLMModel } from '@/screens/ai/api'
import { Empty, ErrorNotice, LINK, Metric, Panel, Progress, Skeleton } from '@/ui/kit'
import { type Column, DataTable } from '@/ui/table'
import { useCountdown, useKeys, useModels, useProviderLabel } from './shared'

interface KeyModelRow {
  key: LLMKey
  model: LLMModel
  /** What this Key may send this model today: the model's limit, or the Key's cap if lower. */
  allowed: number
  /** What is left of that, as the backend counts it: a Key's cap counts every model it serves. */
  remaining: number
  tokens: number
}

export function Budget() {
  const keys = useKeys(60_000)
  const models = useModels()
  // Same key and query as the header clock, so this costs nothing extra.
  const bar = useQuery({
    queryKey: ['bar'],
    queryFn: () => today.bar(),
    refetchInterval: 30_000,
  })
  const providerLabel = useProviderLabel()
  const nextReset = useCountdown(keys.data?.resetInSeconds ?? undefined, keys.dataUpdatedAt)
  const eastern = useCountdown(bar.data?.resetsInSeconds, bar.dataUpdatedAt)

  if (keys.isError) return <ErrorNotice title="Could not load the budget" error={keys.error} />
  if (!keys.data) return <Skeleton className="h-96" />

  const status = keys.data
  const left = status.budget.reduce((s, b) => s + b.remainingToday, 0)

  const perKey: KeyModelRow[] = status.keys
    .filter((k) => k.enabled)
    .flatMap((k) =>
      (models.data?.models ?? [])
        .filter((m) => m.provider === k.provider)
        .map((m) => {
          const usage = k.usage.filter((u) => u.model === m.id)
          return {
            key: k,
            model: m,
            allowed: k.dailyLimit === null ? m.rpd : Math.min(k.dailyLimit, m.rpd),
            remaining: k.remainingToday[m.id] ?? 0,
            tokens: usage.reduce((s, u) => s + u.tokens, 0),
          }
        }),
    )

  const keyColumns: Column<KeyModelRow>[] = [
    {
      key: 'key',
      header: 'Key',
      width: 'minmax(140px,1fr)',
      cell: (r) => (
        <span className="truncate">{r.key.label || <span className="num">{r.key.hint}</span>}</span>
      ),
    },
    {
      key: 'model',
      header: 'Model',
      width: 'minmax(160px,1.2fr)',
      cell: (r) => <span className="num truncate">{r.model.id}</span>,
    },
    {
      key: 'rpd',
      header: 'Requests left today',
      width: 'minmax(200px,1.2fr)',
      cell: (r) => (
        <div className="flex w-full items-center gap-2">
          <Progress
            className="flex-1"
            value={r.remaining / r.allowed}
            label={`${r.model.id} requests left`}
          />
          <span className="num shrink-0 text-body-compact text-ink-muted">
            {fmt.int(r.remaining)} / {fmt.int(r.allowed)}
          </span>
        </div>
      ),
    },
    {
      key: 'rpm',
      header: 'Per Minute',
      width: '90px',
      align: 'right',
      cell: (r) => fmt.int(r.model.rpm),
    },
    {
      key: 'tokens',
      header: 'Tokens today',
      width: '110px',
      align: 'right',
      cell: (r) => fmt.int(r.tokens),
    },
  ]

  return (
    <div className="flex flex-col gap-3">
      <Panel>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric
            label="Assistant requests left today"
            value={fmt.int(left)}
            hint="Every set-up model, across every enabled Key."
          />
          <Metric
            label="Next assistant reset in"
            value={nextReset === undefined ? '—' : fmt.countdown(nextReset)}
            hint="The soonest a model's day turns over. Each resets at midnight in its own time zone."
          />
          <Metric
            label="BRAIN simulations reset in"
            value={bar.isError ? '—' : fmt.countdown(eastern)}
            hint={`00:00 US Eastern. A separate clock: the ${fmt.int(DAILY_SIMULATIONS)} daily simulations.`}
          />
          <Metric
            label="Enabled Keys"
            value={`${fmt.int(status.enabled)} / ${fmt.int(status.keys.length)}`}
            hint="Each Key adds its own daily allowance."
          />
        </div>
      </Panel>

      <Panel
        title="Remaining today, per model"
        description="Requests left across every enabled Key, against what those Keys allow in a day."
      >
        {status.budget.length === 0 ? (
          <Empty title="No budget without a model">
            Set up a model with its limits, and its daily allowance appears here.{' '}
            <Link to="/ai/$tab" params={{ tab: 'models' }} className={LINK}>
              Set up a model
            </Link>
          </Empty>
        ) : (
          <div className="grid grid-cols-1 gap-x-6 gap-y-3 md:grid-cols-2">
            {status.budget.map((b) => (
              <div key={`${b.provider}/${b.model}`} className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between gap-2 text-body-compact">
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="num truncate text-ink">{b.model}</span>
                    <span className="shrink-0 text-ink-subtle">{providerLabel(b.provider)}</span>
                  </span>
                  <span className="num text-ink-muted">
                    {fmt.int(b.remainingToday)} / {fmt.int(b.allowedToday)}
                  </span>
                </div>
                <Progress
                  value={b.allowedToday ? b.remainingToday / b.allowedToday : null}
                  label={`${b.model} requests left`}
                />
                <ResetIn
                  seconds={b.resetInSeconds}
                  fetchedAt={keys.dataUpdatedAt}
                  timeZone={b.resetTimezone}
                />
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="Per Key"
        description="Each enabled Key's allowance for every model set up for its provider. A Key's own daily cap wins where it is lower."
        bodyClassName="p-0"
      >
        {models.isError ? (
          <ErrorNotice className="m-4" title="Could not load the models" error={models.error} />
        ) : (
          <DataTable
            label="Budget per Key and model"
            rows={perKey}
            columns={keyColumns}
            rowKey={(r) => `${r.key.id}/${r.model.id}`}
            loading={models.isLoading}
            empty="No enabled Key has a model set up."
            maxHeight="50vh"
          />
        )}
      </Panel>
    </div>
  )
}

/** One model's countdown to its provider's new day, ticking between refetches. */
function ResetIn({
  seconds,
  fetchedAt,
  timeZone,
}: {
  seconds: number
  fetchedAt: number
  timeZone: string
}) {
  const left = useCountdown(seconds, fetchedAt)
  return (
    <span className="text-body-compact text-ink-subtle">
      Resets in <span className="num">{fmt.countdown(left)}</span> · midnight {timeZone}
    </span>
  )
}
