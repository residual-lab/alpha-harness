/** An Alpha's Simulation Settings as BRAIN lists them, every one, for reading only. */

/** BRAIN's Simulation Settings columns, in its order. */
export const SETTINGS: [string, string][] = [
  ['instrumentType', 'Instrument Type'],
  ['region', 'Region'],
  ['universe', 'Universe'],
  ['language', 'Language'],
  ['decay', 'Decay'],
  ['delay', 'Delay'],
  ['truncation', 'Truncation'],
  ['neutralization', 'Neutralization'],
  ['pasteurization', 'Pasteurization'],
  ['nanHandling', 'NaN Handling'],
  ['unitHandling', 'Unit Handling'],
  ['maxTrade', 'Max Trade'],
  ['maxPosition', 'Max Position'],
]

export function SettingsList({
  settings,
  labels,
  superAlpha = false,
}: {
  settings: Record<string, unknown>
  /** Each setting chosen from a list, by BRAIN's label for it. */
  labels: Record<string, string>
  superAlpha?: boolean
}) {
  const shown: [string, string][] = superAlpha
    ? [...SETTINGS, ['componentActivation', 'Component Activation']]
    : SETTINGS
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-body-compact font-medium text-ink">Simulation Settings</h3>
      {/* Each sized to its own label and value, flowing on as text does: no column is
          squeezed, and none is hidden off to the side. */}
      <dl className="flex flex-wrap gap-x-6 gap-y-3">
        {shown.map(([key, title]) => (
          <div key={key} className="flex flex-col gap-0.5">
            <dt className="text-caption whitespace-nowrap text-ink-subtle">{title}</dt>
            <dd className="num text-body whitespace-nowrap text-ink">
              {labels[key] ?? String(settings[key] ?? '')}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
