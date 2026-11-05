/**
 * One Alpha, showing what BRAIN's own Alpha page shows and nothing more. Submission stays on
 * BRAIN: Submit opens the Alpha there.
 */

import { useQuery } from '@tanstack/react-query'
import { Link, useCanGoBack, useParams, useRouter } from '@tanstack/react-router'
import { ArrowLeftIcon, ExternalLinkIcon } from 'lucide-react'
import { useState } from 'react'
import { DetailSheet } from '@/screens/pool/detail'
import { AlphaActionsMenu, AstInspector, OpenInBrain, RecheckButton } from '@/screens/pool/shared'
import { Badge, Button, ErrorNotice, Notice, Page, Panel, Skeleton, type Tone } from '@/ui/kit'
import { Select } from '@/ui/overlay'
import { type AlphaInfo, alpha as api, type RecordsetName } from './api'
import { platformDay } from './brain'
import { AlphaChart } from './chart'
import {
  Correlation,
  PerformanceComparison,
  RegionalChildren,
  Summary,
  TestingStatus,
} from './panels'
import { PropertiesPanel } from './properties'
import { SettingsList } from './settings-list'

const BACK =
  'inline-flex w-fit items-center gap-1 px-1 text-body-compact text-ink-subtle hover:text-ink'

export function AlphaScreen() {
  const { alphaId } = useParams({ from: '/alpha/$alphaId' })
  const page = useQuery({
    queryKey: ['alpha', alphaId, 'page'],
    queryFn: () => api.page(alphaId),
    retry: false,
  })
  const [stored, setStored] = useState(false)
  // Back to wherever it was opened from; Alphas only when it was opened here directly.
  const canGoBack = useCanGoBack()
  const router = useRouter()

  return (
    <Page className="mx-auto w-full max-w-[96rem]">
      {canGoBack ? (
        <button type="button" onClick={() => router.history.back()} className={BACK}>
          <ArrowLeftIcon className="size-3.5" aria-hidden />
          Back
        </button>
      ) : (
        <Link to="/pool/$tab" params={{ tab: 'stored' }} className={BACK}>
          <ArrowLeftIcon className="size-3.5" aria-hidden />
          Alphas
        </Link>
      )}
      {page.isPending && <Skeleton className="h-[40rem]" label={`Loading ${alphaId} from BRAIN`} />}
      {page.isError && (
        <>
          <ErrorNotice error={page.error} title={`Could not load ${alphaId}`} />
          {/* This screen reads BRAIN; the vault's own copy still opens while BRAIN is away. */}
          <Button className="w-fit" variant="secondary" onClick={() => setStored(true)}>
            Show the stored copy
          </Button>
          <DetailSheet alphaId={stored ? alphaId : null} onClose={() => setStored(false)} />
        </>
      )}
      {page.data && (
        <>
          {page.data.problems.map((p) => (
            <Notice key={p} tone="warn">
              {p}
            </Notice>
          ))}
          {/* Keyed so moving to another Alpha remounts: a running correlation, the shown test
              period and every open section belong to the Alpha they were opened on. */}
          <Body key={alphaId} alpha={page.data.alpha} />
        </>
      )}
    </Page>
  )
}

/** BRAIN's short names for an Alpha's status. */
const STATUS: Record<string, { label: string; tone: Tone }> = {
  UNSUBMITTED: { label: 'UNSUB', tone: 'neutral' },
  SUBMITTED: { label: 'SUB', tone: 'neutral' },
  ACTIVE: { label: 'ACTV', tone: 'profit' },
  DECOMMISSIONED: { label: 'DECM', tone: 'muted' },
}

function Body({ alpha: a }: { alpha: AlphaInfo }) {
  // An unsubmitted Alpha with a test period keeps it hidden until asked, as BRAIN does.
  const testControl = a.status === 'UNSUBMITTED' && Boolean(a.periods.test?.startDate)
  const [testShown, setTestShown] = useState(!testControl)
  const regionAgnostic = a.type === 'RA_PARENT'
  const hidingTest = testControl && !testShown
  return (
    <>
      <Header alpha={a} hidingTest={hidingTest} onShowTest={() => setTestShown(true)} />
      {/* Two columns, as BRAIN lays it out, once each is wide enough for the yearly table. */}
      <div className="@container">
        <div className="grid grid-cols-[minmax(0,1fr)] gap-4 @min-[80rem]:grid-cols-2">
          <div className="flex min-w-0 flex-col gap-4">
            <CodePanel alpha={a} />
            {testControl && (
              <Notice
                tone="warn"
                action={
                  <Button size="sm" onClick={() => setTestShown(!testShown)}>
                    {testShown ? 'Hide test period' : 'Show test period'}
                  </Button>
                }
              >
                Test period and overall stats are hidden by default when test period is specified.
              </Notice>
            )}
            <ChartPanel alpha={a} testShown={testShown} />
            {!hidingTest && (
              // Keyed: showing or hiding the test period opens its sections on TEST or TRAIN.
              <TestingStatus
                key={String(testShown)}
                alpha={a}
                testControl={testControl}
                testShown={testShown}
              />
            )}
          </div>
          <div className="flex min-w-0 flex-col gap-4">
            {!regionAgnostic && (
              <Summary
                key={String(testShown)}
                alpha={a}
                testControl={testControl}
                testShown={testShown}
              />
            )}
            {!regionAgnostic && !hidingTest && <Correlation alpha={a} />}
            {!regionAgnostic && !hidingTest && a.stage === 'IS' && (
              <PerformanceComparison alpha={a} />
            )}
            {/* Both, so the draft resets on a save *and* on a sibling: batch children share a
              dateModified to the second, and a stale draft would save one Alpha's name onto
              another. */}
            <PropertiesPanel key={`${a.alphaId}:${a.dateModified ?? ''}`} alpha={a} />
            {regionAgnostic && <RegionalChildren alpha={a} />}
          </div>
        </div>
      </div>
    </>
  )
}

function Header({
  alpha: a,
  hidingTest,
  onShowTest,
}: {
  alpha: AlphaInfo
  hidingTest: boolean
  onShowTest: () => void
}) {
  const status = STATUS[a.status ?? 'UNSUBMITTED'] ?? { label: a.status ?? '', tone: 'neutral' }
  const unsubmitted = a.status === 'UNSUBMITTED'
  const quick = unsubmitted && a.settings['simulationMode'] === 'QUICK'
  const failing = (a.periods.is?.checks ?? []).some((c) => c.result === 'FAIL')
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 rounded-lg border border-hairline bg-surface-1 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={status.tone}>{status.label}</Badge>
        {a.type && a.type !== 'REGULAR' && <Badge tone="outline">{a.type}</Badge>}
        <span className="text-body-compact text-ink-subtle italic">
          Created {platformDay(a.dateCreated)}
        </span>
      </div>
      <h1 className="min-w-0 text-title text-balance break-words text-ink">
        {a.name ?? 'anonymous'}
      </h1>
      <div className="flex flex-wrap items-center gap-2">
        {a.type === 'RA_CHILD' ? (
          <span className="text-body-compact text-ink-subtle">
            Only the parent region-agnostic alpha can be submitted.
          </span>
        ) : (
          <>
            {hidingTest ? (
              <Button size="sm" onClick={onShowTest}>
                Show test period
              </Button>
            ) : (
              <RecheckButton
                alphaId={a.alphaId}
                label="Check Submission"
                disabled={!unsubmitted || failing || quick}
              />
            )}
            {/* Not ready to submit, it still opens on BRAIN: only the invitation changes. */}
            {unsubmitted && !failing && !quick && !hidingTest ? (
              <Button
                size="sm"
                variant="primary"
                title="Submitting is permanent, so it happens on BRAIN: this opens the Alpha there"
                render={<a href={a.brainUrl} target="_blank" rel="noopener noreferrer" />}
              >
                <ExternalLinkIcon aria-hidden />
                Submit on BRAIN
              </Button>
            ) : (
              <OpenInBrain url={a.brainUrl} />
            )}
          </>
        )}
        <AlphaActionsMenu alphaId={a.alphaId} />
      </div>
    </header>
  )
}

function CodePanel({ alpha: a }: { alpha: AlphaInfo }) {
  return (
    <Panel title="Code" bodyClassName="flex flex-col gap-4">
      {a.type === 'SUPER' ? (
        <>
          <span className="text-body-compact font-medium text-ink-muted">Selection Expression</span>
          <AstInspector expression={a.selection} className="text-body" />
          <span className="text-body-compact font-medium text-ink-muted">Combo Expression</span>
          <AstInspector expression={a.combo} className="text-body" />
        </>
      ) : (
        <AstInspector expression={a.code} className="text-body" />
      )}
      <SettingsList
        settings={a.settings}
        labels={a.settingLabels}
        superAlpha={a.type === 'SUPER'}
      />
    </Panel>
  )
}

/** BRAIN's Performance series, the choices of its Chart. */
const SERIES: { value: RecordsetName; label: string }[] = [
  { value: 'pnl', label: 'PnL' },
  { value: 'sharpe', label: 'Sharpe' },
  { value: 'turnover', label: 'Turnover' },
]

function ChartPanel({ alpha: a, testShown }: { alpha: AlphaInfo; testShown: boolean }) {
  const [series, setSeries] = useState<RecordsetName>('pnl')
  return (
    <Panel
      title="Chart"
      actions={
        <Select label="Chart" value={series} onChange={setSeries} items={SERIES} className="w-40" />
      }
    >
      <AlphaChart
        alphaId={a.alphaId}
        name={series}
        testStart={a.periods.test?.startDate ?? null}
        osStart={a.periods.os?.startDate ?? null}
        testShown={testShown}
      />
    </Panel>
  )
}
