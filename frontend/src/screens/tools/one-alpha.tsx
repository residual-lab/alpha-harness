/**
 * What the Correlation Breaker and the Fields Sampler share: one Alpha, named by its ID, re-run
 * as variants at the settings it was judged on.
 */

import { DASH } from '@/lib/format'
import { alphaIdOf } from '@/screens/pool/api'
import { Button, ErrorNotice, Field, Input, Metric, Notice, Panel } from '@/ui/kit'

/** The settings every variant runs at, and cannot change. */
export interface Held {
  region?: string | null
  delay?: number | null
  universe?: string | null
  neutralization?: string | null
  decay?: number | null
  truncation?: number | null
}

const HELD = [
  { label: 'Region', of: (s: Held) => s.region ?? DASH },
  { label: 'Delay', of: (s: Held) => (s.delay == null ? DASH : `D${s.delay}`) },
  { label: 'Universe', of: (s: Held) => s.universe ?? DASH },
  { label: 'Neutralization', of: (s: Held) => s.neutralization ?? DASH },
  { label: 'Decay', of: (s: Held) => (s.decay == null ? DASH : String(s.decay)) },
  { label: 'Truncation', of: (s: Held) => String(s.truncation ?? DASH) },
] as const

/** The held settings, one boxed figure each, inside a grid that may carry more beside them. */
export function HeldMetrics({ settings }: { settings: Held }) {
  return HELD.map((part) => (
    <Metric key={part.label} boxed size="sm" label={part.label} value={part.of(settings)} />
  ))
}

/** The Alpha ID box, and whatever reading that Alpha found wrong. */
export function AlphaPanel({
  draft,
  setDraft,
  onAnalyse,
  error,
  problems,
}: {
  draft: string
  setDraft: (value: string) => void
  onAnalyse: (event: React.FormEvent) => void
  error: unknown
  problems: readonly string[] | undefined
}) {
  return (
    <Panel title="Alpha">
      <form onSubmit={onAnalyse} className="flex flex-wrap items-end gap-3">
        <Field label="Alpha ID" className="min-w-48 flex-1">
          <Input
            value={draft}
            onChange={(e) => setDraft(alphaIdOf(e.target.value))}
            spellCheck={false}
            className="num"
          />
        </Field>
        <Button type="submit" variant="primary" disabled={!draft.trim()}>
          Analyse
        </Button>
      </form>
      {error != null && <ErrorNotice error={error} title="Could not read this Alpha" />}
      {problems?.map((problem) => (
        <Notice key={problem} tone="error" title={problem} className="mt-3" />
      ))}
    </Panel>
  )
}
