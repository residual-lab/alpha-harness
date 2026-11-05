/** One stored alpha: PnL, settings, submission checks, and correlations on request. */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { MaximizeIcon } from 'lucide-react'
import { useState } from 'react'
import { ApiError, errorMessage } from '@/api/http'
import { fmt, isNum } from '@/lib/format'
import { alpha, notApplicable } from '@/screens/alpha/api'
import { type AlphaSettings, pool } from '@/screens/pool/api'
import { PnlChart } from '@/screens/pool/pnl-chart'
import {
  Badge,
  Button,
  checkTone,
  Empty,
  ErrorNotice,
  KV,
  Metric,
  Notice,
  Skeleton,
} from '@/ui/kit'
import { Sheet, SheetSteps } from '@/ui/overlay'
import { AlphaActionsMenu, AstInspector, checkFigure, OpenInBrain, RecheckButton } from './shared'

type Kind = 'self' | 'prod'
const KIND_LABEL: Record<Kind, string> = {
  self: 'Self-Correlation',
  prod: 'Production Correlation',
}

/** The Alphas among table rows, in order, for {@link useDetailSheet}. */
export const alphaIds = (rows: { alphaId?: string | null | undefined }[]): string[] =>
  rows.flatMap((r) => (r.alphaId ? [r.alphaId] : []))

/** The detail sheet and how to open it: `ids` are the rows it was opened from, in the order
 *  shown, so the sheet can step through them without being closed. */
export function useDetailSheet() {
  const [at, setAt] = useState<{ id: string; ids: string[] } | null>(null)
  return {
    openId: at?.id ?? null,
    open: (id: string, ids: string[] = []) => setAt({ id, ids }),
    sheet: (
      <DetailSheet
        alphaId={at?.id ?? null}
        ids={at?.ids ?? []}
        onStep={(id) => setAt((now) => now && { ...now, id })}
        onClose={() => setAt(null)}
      />
    ),
  }
}

export function DetailSheet({
  alphaId,
  onClose,
  ids = [],
  onStep,
}: {
  alphaId: string | null
  onClose: () => void
  ids?: string[]
  onStep?: (alphaId: string) => void
}) {
  return (
    <Sheet
      open={alphaId !== null}
      onOpenChange={(o) => !o && onClose()}
      title={<span className="num">{alphaId ?? 'Alpha'}</span>}
      actions={onStep && <SheetSteps ids={ids} id={alphaId} onStep={onStep} noun="Alpha" />}
    >
      {alphaId && <Body key={alphaId} alphaId={alphaId} />}
    </Sheet>
  )
}

function Section({
  title,
  description,
  actions,
  children,
}: {
  title: string
  description?: string
  actions?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-3 border-t border-hairline pt-4 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-col gap-0.5">
          <h3 className="text-title">{title}</h3>
          {description && <p className="text-body-compact text-ink-subtle">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  )
}

function Body({ alphaId }: { alphaId: string }) {
  const detail = useQuery({
    queryKey: ['pool', 'detail', alphaId],
    queryFn: () => pool.detail(alphaId),
    retry: false,
  })
  const [kinds, setKinds] = useState<Kind[]>([])

  if (detail.isPending) return <Skeleton className="h-60" />
  if (detail.isError) return <ErrorNotice error={detail.error} title="Could not load this Alpha" />
  const d = detail.data

  return (
    <div className="flex flex-col gap-4">
      <AstInspector expression={d.expression} />
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="primary"
          render={<Link to="/alpha/$alphaId" params={{ alphaId: d.alphaId }} />}
        >
          <MaximizeIcon aria-hidden />
          Open Full Page
        </Button>
        <OpenInBrain url={d.brainUrl} />
        <RecheckButton alphaId={d.alphaId} />
        <AlphaActionsMenu alphaId={d.alphaId} />
      </div>

      <PnlSection alphaId={d.alphaId} />

      <Section title="Settings">
        <KV items={settingsItems(d.settings)} />
      </Section>

      <Section
        title="Submission Checks"
        description="As BRAIN last reported them. Pending checks resolve with Re-check on BRAIN."
      >
        {d.checks.length === 0 ? (
          <p className="text-body-compact text-ink-subtle">No checks stored.</p>
        ) : (
          <ul className="flex flex-col divide-y divide-hairline-subtle">
            {d.checks.map((c, i) => (
              <li
                key={`${c.name}-${i}`}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-1.5 text-body"
              >
                <Badge tone={checkTone(c.result ?? 'PENDING')}>{c.result ?? 'PENDING'}</Badge>
                <span className="num text-ink">{c.name}</span>
                {isNum(c.value) && (
                  <span className="num text-body-compact text-ink-subtle">
                    {checkFigure(c.name, c.value)}
                    {isNum(c.limit) && ` / ${checkFigure(c.name, c.limit)}`}
                  </span>
                )}
                {c.message && (
                  <span className="basis-full text-body-compact break-words text-ink-subtle">
                    {c.message}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section
        title="Correlations"
        description="BRAIN rate-limits these checks hourly, so each one loads only when you ask."
        actions={(['self', 'prod'] as Kind[]).map((k) => (
          <Button
            key={k}
            size="sm"
            disabled={kinds.includes(k)}
            onClick={() => setKinds([...kinds, k])}
          >
            {KIND_LABEL[k]}
          </Button>
        ))}
      >
        {kinds.map((k) => (
          <CorrelationResult key={k} alphaId={alphaId} kind={k} />
        ))}
      </Section>
    </div>
  )
}

/** Its own request: the first view of an Alpha downloads its daily PnL from BRAIN, and the
 * rest of the sheet, all stored locally, should not wait for that. */
function PnlSection({ alphaId }: { alphaId: string }) {
  const q = useQuery({
    queryKey: ['pool', 'pnl', alphaId],
    queryFn: () => pool.pnl(alphaId),
    retry: false,
  })
  const p = q.data
  return (
    <Section
      title="Cumulative PnL"
      description={p ? `${fmt.int(p.days)} trading days stored` : 'Reading the daily PnL…'}
    >
      {q.isPending ? (
        <Skeleton className="h-48" label="Downloading the daily PnL from BRAIN" />
      ) : q.isError ? (
        <ErrorNotice error={q.error} title="Could not load the daily PnL" />
      ) : (
        <>
          {q.data.problem && <Notice tone="warn">{q.data.problem}</Notice>}
          {q.data.pnl.length > 1 ? (
            <PnlChart
              values={q.data.pnl}
              dates={q.data.dates}
              label={`Cumulative PnL of ${alphaId}`}
            />
          ) : (
            !q.data.problem && (
              <Empty title="No daily PnL stored">BRAIN returned no daily PnL for this Alpha.</Empty>
            )
          )}
        </>
      )}
    </Section>
  )
}

const cellText = (v: unknown) =>
  isNum(v) ? (Number.isInteger(v) ? fmt.int(v) : fmt.ratio(v, 4)) : v == null ? '—' : String(v)

function CorrelationResult({ alphaId, kind }: { alphaId: string; kind: Kind }) {
  const q = useQuery({
    queryKey: ['pool', 'correlations', alphaId, kind],
    queryFn: () => alpha.correlation(alphaId, kind, 'run'),
    retry: false,
    staleTime: Infinity,
  })
  const label = KIND_LABEL[kind]

  if (q.isPending) return <Skeleton className="h-24" />
  if (q.isError) {
    const e = q.error
    if (notApplicable(e)) {
      return (
        <p className="text-body-compact text-ink-subtle">{label}: not applicable to this Alpha.</p>
      )
    }
    const limited = e instanceof ApiError && e.code === 'rate_limited'
    const retry = (
      <Button size="sm" variant="ghost" onClick={() => q.refetch()}>
        Try again
      </Button>
    )
    return limited ? (
      <Notice tone="warn" title={`${label}: BRAIN's hourly limit`} action={retry}>
        {errorMessage(e)}
        {isNum(e.body.retryAfter) && (
          <>
            {' '}
            Try again in <span className="num">{fmt.duration(e.body.retryAfter)}</span>.
          </>
        )}
      </Notice>
    ) : (
      <div className="flex flex-col gap-2">
        <ErrorNotice error={e} title={label} />
        <div>{retry}</div>
      </div>
    )
  }

  // A run always comes back kept; `cached: false` only answers a cached-only read.
  if (!q.data.cached) return null
  const props = q.data.schema?.properties ?? []
  const rows = q.data.records ?? []

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-3">
        <h3 className="text-body font-medium text-balance text-ink">{label}</h3>
        {/* A kept answer: self-correlation moves as other Alphas are submitted. */}
        <span className="text-body-compact text-ink-subtle">{fmt.ago(q.data.fetchedAt)}</span>
        {isNum(q.data.min) && <Metric size="sm" label="Min" value={fmt.ratio(q.data.min, 4)} />}
        {isNum(q.data.max) && <Metric size="sm" label="Max" value={fmt.ratio(q.data.max, 4)} />}
      </div>
      {rows.length === 0 ? (
        <p className="text-body-compact text-ink-subtle">BRAIN returned no rows.</p>
      ) : (
        <div className="max-h-64 overflow-auto rounded-md border border-hairline">
          <table className="w-full text-body">
            <thead className="sticky top-0 bg-surface-1">
              <tr>
                {props.map((p) => (
                  <th
                    key={p.name}
                    className="border-b border-hairline px-3 py-1.5 text-left text-body-compact font-medium text-ink-subtle"
                  >
                    {p.title ?? p.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((record, i) => (
                <tr key={i} className="border-b border-hairline-subtle last:border-b-0">
                  {props.map((p, j) => (
                    <td
                      key={p.name}
                      className={`num px-3 py-1 ${isNum(record[j]) ? 'text-right' : ''}`}
                    >
                      {cellText(record[j])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

const settingsItems = (s: AlphaSettings): [string, string][] => [
  ['Region', s.region ?? '—'],
  ['Universe', s.universe ?? '—'],
  ['Delay', fmt.int(s.delay)],
  ['Neutralization', s.neutralization ?? '—'],
  ['Decay', fmt.int(s.decay)],
  ['Truncation', fmt.ratio(s.truncation)],
]
