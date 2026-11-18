/** The neutralization chooser every lab shares: two families, pickable whole or one at a time. */

import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { neutralizationLabel, splitNeutralizations } from '@/lib/neutralization'
import type { Choice } from '@/lib/scope'
import { Button, Chips, Fieldset } from '@/ui/kit'

/** Named the way people ask for it, so it reads as the fix for too much industry exposure. */
const RISK_TITLE = 'Risk (FAST, SLOW, … RAM)'

/**
 * Every neutralization the market offers, grouped, with a button per family that ticks or
 * unticks the whole of it.
 *
 * The families are a shortcut, never a restriction: both can be on at once, and any hand-picked
 * mix is legal. A sweep across the risk models and a sweep across the group neutralizations are
 * different experiments, which is what makes the two buttons worth having — but nothing here
 * stops a run that mixes them.
 */
export function NeutralizationPicker({
  available,
  value,
  onChange,
  legend = 'Neutralization',
  hint,
  disabled,
}: {
  /** The legal values for the scope, from BRAIN's settings schema. */
  available: Choice[]
  value: string[]
  onChange: (value: string[]) => void
  legend?: string
  /** A note under the picker, e.g. what is missing from the choice. */
  hint?: string | undefined
  disabled?: boolean | undefined
}) {
  const blocks = splitNeutralizations(available)
  const chosen = new Set(value)
  // The order BRAIN gave, so a selection reads the same way wherever it is shown.
  const order = available.map((choice) => choice.value)
  const sorted = (values: Iterable<string>) =>
    [...new Set(values)].sort((a, b) => order.indexOf(a) - order.indexOf(b))
  // All of them on already means the job is to take them off again.
  const toggle = (all: boolean, ids: string[]) =>
    onChange(all ? value.filter((v) => !ids.includes(v)) : sorted([...value, ...ids]))

  return (
    <Fieldset legend={legend} hint={hint}>
      <div className="flex flex-col gap-3">
        {blocks.map(({ group, items }) => {
          const ids = items.map((item) => item.value)
          const on = ids.filter((id) => chosen.has(id)).length
          const all = on === ids.length
          return (
            // A box each, so which family a value belongs to is read from where it sits
            // rather than from how far it is under a heading.
            <div
              key={group.id}
              className={cn(
                'flex min-w-0 flex-col rounded-md border transition-colors',
                on > 0 ? 'border-hairline-strong bg-surface-1' : 'border-hairline bg-surface-1',
              )}
            >
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-hairline-subtle px-3 py-2">
                <div className="flex min-w-0 items-baseline gap-2">
                  {/* The title toggles the family too: a heading over a set of checkboxes is
                      the thing people reach for, and the button beside it is what tells them
                      the heading can be reached for at all. */}
                  <button
                    type="button"
                    disabled={disabled}
                    aria-pressed={all}
                    onClick={() => toggle(all, ids)}
                    className="text-body font-medium text-ink transition-colors hover:text-link disabled:text-(--field-text-disabled)"
                  >
                    {group.id === 'risk' ? RISK_TITLE : group.label}
                  </button>
                  <span
                    className={cn(
                      'num text-caption',
                      on > 0 ? 'text-ink-muted' : 'text-ink-subtle',
                    )}
                  >
                    {fmt.int(on)} of {fmt.int(ids.length)}
                  </span>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={disabled}
                  // Says what pressing it does, rather than lighting up to report a state the
                  // count beside it already gives.
                  onClick={() => toggle(all, ids)}
                >
                  {all ? 'Clear' : 'Select all'}
                </Button>
              </div>
              <div className="p-3">
                <Chips
                  label={group.label}
                  disabled={disabled}
                  value={ids.filter((id) => chosen.has(id))}
                  onChange={(next) =>
                    onChange(sorted([...value.filter((v) => !ids.includes(v)), ...next]))
                  }
                  items={items.map((item) => ({
                    value: item.value,
                    label: neutralizationLabel(item.value, item.label),
                  }))}
                />
              </div>
            </div>
          )
        })}
      </div>
    </Fieldset>
  )
}
