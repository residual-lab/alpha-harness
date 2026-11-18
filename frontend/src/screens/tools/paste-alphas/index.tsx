/**
 * Paste Alphas (Tools): the Alphas an LLM wrote, each run at the settings it was written with.
 *
 * The output, pasted here, becomes one task. Every Alpha is shown as it will run before
 * anything is queued, with what took BRAIN's default and what stops it running said beside it.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { useDebounced } from '@/lib/use-debounced'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import { CoresSetting } from '@/screens/research-labs/task-settings'
import { Empty, ErrorNotice, Notice, Page, PageHeader, Panel, Textarea } from '@/ui/kit'
import { type PastedAlpha, pasteAlphas } from './api'

/** What a pasted Alpha shows of its settings, in the order Copy Alphas writes them. */
const SHOWN: [string, string][] = [
  ['region', 'Region'],
  ['universe', 'Universe'],
  ['delay', 'Delay'],
  ['decay', 'Decay'],
  ['truncation', 'Truncation'],
  ['neutralization', 'Neutralization'],
  ['pasteurization', 'Pasteurization'],
  ['nanHandling', 'NaN Handling'],
  ['maxTrade', 'Max Trade'],
  ['maxPosition', 'Max Position'],
  ['testPeriod', 'Test Period'],
]

function settingsLine(alpha: PastedAlpha) {
  return SHOWN.filter(([key]) => alpha.settings[key] != null)
    .map(([key, title]) => `${title} ${alpha.labels[key] ?? String(alpha.settings[key])}`)
    .join(' · ')
}

function AlphaCard({ alpha }: { alpha: PastedAlpha }) {
  const blocked = alpha.problems.length > 0
  const skipped = blocked || alpha.duplicateOf != null
  return (
    <li
      className={cn(
        'rounded-md border p-3',
        skipped ? 'border-hairline bg-surface-1 opacity-70' : 'border-hairline bg-surface-2',
      )}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="font-medium text-ink">Alpha {fmt.int(alpha.number)}</span>
        {blocked ? (
          <span className="text-body-compact text-status-warning">Will not run</span>
        ) : alpha.duplicateOf != null ? (
          <span className="text-body-compact text-ink-subtle">
            Same as Alpha {fmt.int(alpha.duplicateOf)}, run once
          </span>
        ) : null}
      </div>
      {alpha.expression && (
        <pre className="num mt-2 overflow-x-auto rounded-sm border border-hairline bg-canvas p-3 text-body-compact whitespace-pre text-ink-muted">
          {alpha.expression}
        </pre>
      )}
      <p className="num mt-2 text-body-compact text-pretty text-ink-subtle">
        {settingsLine(alpha)}
      </p>
      {alpha.defaulted.length > 0 && !blocked && (
        <p className="mt-1 text-body-compact text-pretty text-ink-subtle">
          Not given, so BRAIN&rsquo;s default: {alpha.defaulted.join(', ')}
        </p>
      )}
      {alpha.categories.length > 2 && (
        <p className="mt-1 text-body-compact text-pretty text-status-warning">
          Reads {alpha.categories.length} Dataset Categories ({alpha.categories.join(', ')}): BRAIN
          counts an Alpha over 2 toward no Genius pyramid, though it still counts toward base
          payment.
        </p>
      )}
      {alpha.problems.map((problem) => (
        <p key={problem} className="mt-1 text-body-compact text-pretty text-status-warning">
          {problem}
        </p>
      ))}
    </li>
  )
}

export function PasteAlphasScreen() {
  const [text, setText] = useState('')
  /** `null` until chosen here: until then Settings' default for new tasks applies. */
  const [chosenCores, setCores] = useState<number | null>(null)
  const cores = useCores(chosenCores)
  const typed = useDebounced(text.trim(), 300)

  const query = useQuery({
    queryKey: ['paste-alphas', typed],
    queryFn: ({ signal }) => pasteAlphas.preview({ text: typed, cores: 1 }, signal),
    enabled: typed.length > 0,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const plan = typed ? query.data : undefined
  const add = useAddTask(() => pasteAlphas.addTask({ text: typed, cores }))

  const blocked = plan?.alphas.filter((a) => a.problems.length > 0).length ?? 0
  const repeated = plan?.alphas.filter((a) => a.duplicateOf != null).length ?? 0
  const current = typed === text.trim()

  return (
    <Page>
      <PageHeader
        sticky
        title="Paste Alphas"
        description="Simulate the Alphas an LLM wrote, each at the Simulation Settings it gave"
        actions={
          <AddTaskButtons
            add={add}
            disabled={!plan || plan.simulations === 0 || plan.problems.length > 0 || !current}
            blocked={typed ? (plan?.problems[0] ?? (current ? null : 'Reading the output.')) : null}
          />
        }
      />

      <Panel title="LLM Output">
        <Textarea
          className="font-mono"
          rows={14}
          spellCheck={false}
          aria-label="LLM Output"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </Panel>

      {query.isError && typed && (
        <ErrorNotice error={query.error} title="Could not read the output" />
      )}

      {plan && (
        <Panel
          title="Alphas"
          description={
            plan.simulations > 0
              ? `${fmt.int(plan.simulations)} of ${fmt.int(plan.alphas.length)} will run, packed into ${fmt.int(plan.batches)} Multi-Simulation${plan.batches === 1 ? '' : 's'}, the slowest markets first.`
              : `${fmt.int(plan.alphas.length)} found, none of which can run as written.`
          }
          actions={<CoresSetting value={cores} onChange={setCores} />}
        >
          <div className="flex flex-col gap-4">
            {plan.problems.map((problem) => (
              <Notice key={problem} tone="warn">
                {problem}
              </Notice>
            ))}
            {(blocked > 0 || repeated > 0) && (
              <p className="text-body-compact text-ink-subtle">
                {[
                  blocked && `${fmt.int(blocked)} will not run, each with its reason below`,
                  repeated && `${fmt.int(repeated)} repeat an earlier one and run once`,
                ]
                  .filter(Boolean)
                  .join(' · ')}
                .
              </p>
            )}
            {plan.alphas.length === 0 ? (
              <Empty title="No Alphas found" />
            ) : (
              <ul className="flex flex-col gap-3">
                {plan.alphas.map((alpha) => (
                  <AlphaCard key={alpha.number} alpha={alpha} />
                ))}
              </ul>
            )}
            <p className="text-body-compact text-pretty text-ink-subtle">
              When the task has run, <span className="text-ink">Copy Alphas</span> on it in Tasks
              copies the results back in the same shape, each with how BRAIN&rsquo;s checks judged
              it, ready for the next output.
            </p>
          </div>
        </Panel>
      )}
    </Page>
  )
}
