/** How to write a Selection Expression, in the side pane beside its editor. */

import { InfoButton, InfoSection } from '@/ui/info'
import { FUNCTIONS } from './selection'

const EXAMPLE = 'information_ratio >= 0.10;\nreturns / turnover >= 1;\nreturns / drawdown >= 1'

const List = ({ names }: { names: string[] }) => (
  <ul className="flex flex-wrap gap-x-3 gap-y-1">
    {names.map((n) => (
      <li key={n} className="num text-ink">
        {n}
      </li>
    ))}
  </ul>
)

export function SelectionInfo({ metrics }: { metrics: string[] }) {
  return (
    <InfoButton
      title="Selection Expression"
      description="Fast Expression syntax over each Alpha's metrics."
    >
      <InfoSection title="How it reads">
        <p>
          Each statement ends at a new line or <code className="num text-ink">;</code>. Every
          statement is a condition, and an Alpha is selected when it meets them all.
        </p>
        <p>
          <code className="num text-ink">name = …</code> names a value for the lines after it.
        </p>
        <p>
          <code className="num text-ink">&amp;&amp;</code>, <code className="num text-ink">||</code>{' '}
          and <code className="num text-ink">!</code> are <code className="num text-ink">and</code>,{' '}
          <code className="num text-ink">or</code> and <code className="num text-ink">not</code>.{' '}
          <code className="num text-ink">#</code> or <code className="num text-ink">{'//'}</code>{' '}
          starts a comment.
        </p>
        <pre className="num rounded-md bg-surface-2 p-3 text-ink">{EXAMPLE}</pre>
      </InfoSection>

      <InfoSection title="Metrics">
        <p>
          BRAIN's own figures for each Alpha, as BRAIN reports them, whatever the cost set on this
          page. In BRAIN's units: Returns of 10% is <span className="num text-ink">0.10</span>, or
          write <span className="num text-ink">10%</span>; a Margin of{' '}
          <span className="num text-ink">5bps</span> is <span className="num text-ink">0.0005</span>
          .
        </p>
        <p>
          <span className="num text-ink">information_ratio</span> is Sharpe /{' '}
          <span className="num text-ink">√250</span>, the days BRAIN annualizes over, so it is as
          exact as BRAIN's two-place Sharpe.
        </p>
        <p>An Alpha missing a metric fails any condition on it.</p>
        <List names={metrics} />
      </InfoSection>

      <InfoSection title="Functions">
        <List names={Object.keys(FUNCTIONS)} />
      </InfoSection>
    </InfoButton>
  )
}
