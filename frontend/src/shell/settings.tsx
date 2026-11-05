/**
 * Settings: floats over whatever screen is open, like ⌘K, so changing a choice never costs the
 * place you were at. Sections down the left; each row says what it does and changes it at once.
 */

import { Dialog as BDialog } from '@base-ui/react/dialog'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckIcon,
  CpuIcon,
  DownloadIcon,
  LayersIcon,
  ListChecksIcon,
  LoaderCircleIcon,
  type LucideIcon,
  RefreshCwIcon,
  XIcon,
} from 'lucide-react'
import { type ReactNode, useEffect, useId, useRef, useState } from 'react'
import { toast } from 'sonner'
import { create } from 'zustand'
import { type Preferences, simulations, update } from '@/api/core'
import type { components } from '@/api/generated'
import { errorMessage } from '@/api/http'
import { REGION_AGNOSTIC_REGION, SIMULATION_SLOTS } from '@/api/platform'
import { fmt } from '@/lib/format'
import { usePreferences, useSavePreferences } from '@/lib/preferences'
import { DEFAULT_SCOPE, useScopeOptions } from '@/lib/scope'
import { useProviderLabel, useRunnableModels } from '@/screens/ai/shared'
import { Button, Chips, ErrorNotice, Segmented, Skeleton, Switch } from '@/ui/kit'
import { BACKDROP, Select } from '@/ui/overlay'
import { MOD } from './desktop'
import { useApplyUpdate, useUpdateStatus } from './update'

export const useSettings = create<{
  open: boolean
  setOpen: (open: boolean) => void
}>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}))

export const SETTINGS_SHORTCUT = `${MOD},`

type SectionId = 'simulations' | 'tasks' | 'alphas' | 'updates'

const SECTIONS: { id: SectionId; label: string; icon: LucideIcon }[] = [
  { id: 'simulations', label: 'Simulations', icon: CpuIcon },
  { id: 'tasks', label: 'Tasks', icon: ListChecksIcon },
  { id: 'alphas', label: 'Alphas', icon: LayersIcon },
  { id: 'updates', label: 'Updates', icon: RefreshCwIcon },
]

export function SettingsDialog() {
  const { open, setOpen } = useSettings()
  const [section, setSection] = useState<SectionId>('simulations')
  // Focus lands on the section's content, not the first nav item: a ring there read as a
  // second selection.
  const content = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === ',') {
        event.preventDefault()
        setOpen(!useSettings.getState().open)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setOpen])

  return (
    <BDialog.Root open={open} onOpenChange={(next) => setOpen(next)}>
      <BDialog.Portal>
        <BDialog.Backdrop className={BACKDROP} />
        <BDialog.Popup
          initialFocus={content}
          className="fixed top-1/2 left-1/2 z-50 flex h-[min(40rem,calc(100vh-4rem))] w-[calc(100%-2rem)] max-w-4xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-lg border border-hairline-strong bg-surface-2 shadow-float outline-none sm:flex-row"
        >
          <nav
            aria-label="Settings sections"
            className="flex shrink-0 flex-col gap-0.5 border-b border-hairline p-2 sm:w-56 sm:border-r sm:border-b-0"
          >
            <BDialog.Title className="px-2 pt-1 pb-2 text-caption font-medium text-ink-subtle">
              Settings
            </BDialog.Title>
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                type="button"
                aria-current={s.id === section ? 'true' : undefined}
                onClick={() => setSection(s.id)}
                className="flex h-8 items-center gap-3 rounded-md px-2 text-left text-body text-ink-muted transition-colors hover:bg-surface-3 hover:text-ink aria-[current=true]:bg-surface-4 aria-[current=true]:font-medium aria-[current=true]:text-ink"
              >
                <s.icon className="size-4 shrink-0" aria-hidden />
                {s.label}
              </button>
            ))}
          </nav>
          <div
            ref={content}
            tabIndex={-1}
            className="relative min-h-0 flex-1 overflow-y-auto px-6 py-5 outline-none"
          >
            <BDialog.Close
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Close settings"
                  className="absolute top-3 right-3"
                />
              }
            >
              <XIcon />
            </BDialog.Close>
            <Section id={section} />
          </div>
        </BDialog.Popup>
      </BDialog.Portal>
    </BDialog.Root>
  )
}

const ABOUT: Record<SectionId, string> = {
  simulations: 'How simulations run, share the eight cores, and keep going through the day.',
  tasks: 'What a new task in any lab or tool starts with.',
  alphas:
    'What is kept on this computer as Alphas come back from BRAIN, and which model writes their descriptions.',
  updates: 'How Alpha Harness finds and installs new versions.',
}

function Section({ id }: { id: SectionId }) {
  const saved = usePreferences()
  const save = useSavePreferences()
  const current = saved.data
  const title = SECTIONS.find((s) => s.id === id)?.label ?? ''
  const change = (patch: Partial<Preferences>) => current && save.mutate({ ...current, ...patch })

  return (
    <section className="flex flex-col gap-1">
      <h2 className="pr-10 text-title font-medium text-ink">{title}</h2>
      <p className="text-body-compact text-ink-subtle">{ABOUT[id]}</p>
      <div className="mt-3 flex flex-col">
        {saved.isError ? (
          <ErrorNotice error={saved.error} title="Settings could not load" />
        ) : !current ? (
          <Skeleton className="h-14" label="Loading settings" />
        ) : id === 'simulations' ? (
          <SimulationRows current={current} change={change} />
        ) : id === 'tasks' ? (
          <TaskRows current={current} change={change} />
        ) : id === 'alphas' ? (
          <AlphaRows current={current} change={change} />
        ) : (
          <UpdateRows current={current} change={change} />
        )}
      </div>
    </section>
  )
}

interface Rows {
  current: Preferences
  change: (patch: Partial<Preferences>) => void
}

// ── Simulations ──────────────────────────────────────────────────────────────────────────

const AWAKE: Record<components['schemas']['EngineStatus']['awake'], string> = {
  held: 'Holding it awake now: simulations are pending.',
  idle: 'Nothing is pending right now, so it may sleep.',
  unavailable:
    'This system does not let an app keep it awake (WSL, for one). Keep it on yourself while simulations run.',
}

const LIST_FORMAT = new Intl.ListFormat('en', { type: 'conjunction' })

function SimulationRows({ current, change }: Rows) {
  const engine = useQuery({ queryKey: ['simulations', 'engine'], queryFn: simulations.engine })
  const awake = engine.data?.awake
  // Region-agnostic runs are scored as a family of regions, which Quick mode has not been
  // tried against, so only single regions are offered.
  const regions = useScopeOptions(DEFAULT_SCOPE).regions.filter(
    (r) => r.value !== REGION_AGNOSTIC_REGION,
  )
  const quick = current.quickRegions ?? []
  const quickAllowed = engine.data?.quickAllowed ?? true
  return (
    <>
      <SettingRow
        title="Quick mode"
        description="Simulates with only the checks that score an Alpha, leaving out correlation, Theme and Competition. The figures are the same as in full, and a GLB batch finished 30% sooner. A Quick Alpha cannot be submitted, so each one that passes every check is simulated again in full, which costs one more simulation."
        control={() => (
          <Chips
            label="Regions that simulate in Quick mode"
            items={ticked(regions, quick)}
            value={quick}
            disabled={!quickAllowed}
            onChange={(next) => change({ quickRegions: next })}
          />
        )}
        stacked
      >
        <Status>
          {!quickAllowed
            ? 'Your BRAIN account does not have Quick mode, so every simulation runs in full.'
            : quick.length === 0
              ? 'Off: every simulation runs in full.'
              : `Quick in ${LIST_FORMAT.format(quick)}. Every other region runs in full.`}
        </Status>
      </SettingRow>
      <SettingRow
        title="Lend idle cores"
        description="Running tasks borrow the cores no task holds, so a one-core task can use all eight while nothing else is queued. A task you start later gets its own cores first, once the borrowed batches come back: a few minutes at most. Off, every task stays within its own cores."
        control={(labels) => (
          <Switch
            checked={current.lendIdleCores}
            onChange={(on) => change({ lendIdleCores: on })}
            {...labels}
          />
        )}
      />
      <SettingRow
        title="Keep the computer awake"
        description="While simulations are pending, asks the computer not to go to sleep: a sleeping computer sends nothing, and quota left unspent at the daily reset is lost. Closing a laptop's lid still puts it to sleep."
        control={(labels) => (
          <Switch
            checked={current.keepAwake}
            onChange={(on) => change({ keepAwake: on })}
            {...labels}
          />
        )}
      >
        <Status>
          {!current.keepAwake
            ? 'Off: the computer may sleep, and simulations wait until it wakes.'
            : awake
              ? AWAKE[awake]
              : 'Reading what the computer allows…'}
        </Status>
      </SettingRow>
    </>
  )
}

// ── Tasks ────────────────────────────────────────────────────────────────────────────────

const CORES = Array.from({ length: SIMULATION_SLOTS }, (_, i) => ({
  value: i + 1,
  label: String(i + 1),
}))

function TaskRows({ current, change }: Rows) {
  return (
    <SettingRow
      title="Default cores for new tasks"
      description="Every lab and tool starts a new task with this many cores. Change the cores in a lab's own form to use a different number there; that form then keeps its own choice."
      control={() => (
        <Segmented
          label="Default cores for new tasks"
          items={CORES}
          value={current.defaultCores}
          onChange={(cores) => change({ defaultCores: cores })}
        />
      )}
      stacked
    />
  )
}

// ── Alphas ───────────────────────────────────────────────────────────────────────────────

type CheckResult = components['schemas']['CheckResult']

/** In the order a consultant reads them: best to worst. */
const RESULTS: { value: CheckResult; label: string; title: string }[] = [
  { value: 'PASS', label: 'Pass', title: 'The Alpha met the check.' },
  {
    value: 'WARNING',
    label: 'Warning',
    title: 'Missed a bar the Alpha can still be submitted without, or is waiting on other checks.',
  },
  { value: 'PENDING', label: 'Pending', title: 'Not decided yet: BRAIN has not run it.' },
  { value: 'ERROR', label: 'Error', title: 'BRAIN could not run the check.' },
  { value: 'FAIL', label: 'Fail', title: 'Refused: the Alpha cannot be submitted as it is.' },
]

const DEFAULT_RESULTS: CheckResult[] = ['PASS', 'WARNING', 'PENDING']

function describeResults(chosen: CheckResult[]): string {
  const names = RESULTS.filter((r) => chosen.includes(r.value)).map((r) => r.label.toLowerCase())
  if (names.length === RESULTS.length) return 'Every Alpha, whatever its checks say.'
  if (names.length === 1 && chosen[0] === 'PASS') return 'Only Alphas that passed every check.'
  const same =
    chosen.length === DEFAULT_RESULTS.length && DEFAULT_RESULTS.every((r) => chosen.includes(r))
  if (same)
    return 'The Alphas nothing has refused: every check passed, warned or is still pending. This is the default.'
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} or ${names.at(-1)}`
  return `Alphas whose every check is ${list}.`
}

function AlphaRows({ current, change }: Rows) {
  return (
    <>
      <PnlRow current={current} change={change} />
      <DescriptionModelRow current={current} change={change} />
    </>
  )
}

function PnlRow({ current, change }: Rows) {
  const chosen = current.pnlCheckResults ?? DEFAULT_RESULTS
  return (
    <SettingRow
      title="Download PnL as Alphas land"
      description="Saves each new Alpha's daily PnL on this computer as soon as its simulation finishes, so charts and correlations never wait for it. Each costs about three BRAIN requests. PnL a screen needs is still downloaded when you open it, whatever you choose here."
      control={(labels) => (
        <Switch
          checked={current.pnlDownload}
          onChange={(on) => change({ pnlDownload: on })}
          {...labels}
        />
      )}
    >
      <div className="flex flex-col gap-2 rounded-md border border-hairline bg-surface-1 p-3">
        <span className="text-body-compact font-medium text-ink-muted">
          Only when every check is
        </span>
        <Chips
          label="Check results that allow the download"
          items={ticked(RESULTS, chosen)}
          value={chosen}
          disabled={!current.pnlDownload}
          // Nothing chosen would download nothing; turning it off says that plainly.
          onChange={(next) => next.length > 0 && change({ pnlCheckResults: next })}
        />
        <Status>
          {current.pnlDownload ? describeResults(chosen) : 'Off: nothing downloads by itself.'}{' '}
          Checks that only label an Alpha, such as Pyramid, Theme or Competition matches, do not
          count.
        </Status>
      </div>
    </SettingRow>
  )
}

/** The Select's value for no choice: model refs always hold a colon, so it names none. */
const AUTOMATIC = 'automatic'

function DescriptionModelRow({ current, change }: Rows) {
  const { runnable, all, ready } = useRunnableModels()
  const providerLabel = useProviderLabel()
  const named = (m: { id: string; provider: string }) => `${m.id} · ${providerLabel(m.provider)}`
  const chosen = current.descriptionModel ?? AUTOMATIC
  const kept = all.find((m) => m.ref === chosen)
  const runs = chosen === AUTOMATIC || runnable.some((m) => m.ref === chosen)
  const items = [
    { value: AUTOMATIC, label: 'Automatic' },
    ...runnable.map((m) => ({ value: m.ref, label: named(m) })),
    // A choice that can no longer run keeps its place, so it reads as it was made, and why.
    ...(runs
      ? []
      : [
          {
            value: chosen,
            label: kept ? `${named(kept)} (no enabled Key)` : `${chosen} (no longer set up)`,
          },
        ]),
  ]
  const [busiest] = runnable
  const status = !ready
    ? null
    : !busiest
      ? 'Off: none of your Keys can run a model. Add a Key and a model under LLM Integration.'
      : !runs
        ? kept
          ? 'It has no enabled Key, so Write with AI is off. Enable one under LLM Integration.'
          : 'It is no longer set up, so Write with AI is off. Choose another model.'
        : chosen === AUTOMATIC
          ? `Automatic uses ${named(busiest)}: most requests left today.`
          : null
  return (
    <SettingRow
      title="Power Pool Alpha Descriptions"
      description={
        // A sentence a line, each short enough not to wrap in the dialog.
        <>
          <span className="block">
            Writes a Power Pool Alpha's description when you press Write with AI on its page.
          </span>
          <span className="block">
            It sees only that Alpha's expression and the fields and operators it uses.
          </span>
        </>
      }
      control={() => (
        <Select
          label="Model for Power Pool Alpha Descriptions"
          items={items}
          value={chosen}
          className="w-96"
          disabled={ready && !busiest}
          onChange={(next) => change({ descriptionModel: next === AUTOMATIC ? null : next })}
        />
      )}
      stacked
    >
      {status && <Status>{status}</Status>}
    </SettingRow>
  )
}

// ── Updates ──────────────────────────────────────────────────────────────────────────────

const CHECK_EVERY: { value: string; label: string }[] = [
  { value: '1', label: 'Every hour' },
  { value: '6', label: 'Every 6 hours' },
  { value: '24', label: 'Once a day' },
  { value: '0', label: 'Only when I ask' },
]

function UpdateRows({ current, change }: Rows) {
  const queryClient = useQueryClient()
  const status = useUpdateStatus()
  const apply = useApplyUpdate()
  const [checking, setChecking] = useState(false)
  const data = status.data

  const checkNow = async () => {
    setChecking(true)
    try {
      queryClient.setQueryData(['update'], await update.status(true))
    } catch (error) {
      toast.error('Could not check for updates', { description: errorMessage(error) })
    } finally {
      setChecking(false)
    }
  }

  return (
    <>
      <div className="flex flex-col gap-3 border-t border-hairline py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="text-body font-medium text-ink">
              {data?.isRelease ? `Alpha Harness ${data.current}` : 'Development build'}
            </span>
            <Status>
              {!data
                ? 'Reading the version…'
                : !data.isRelease
                  ? 'Updates apply to installed releases only.'
                  : data.available
                    ? `${data.latest} is out.`
                    : data.checkedAt
                      ? `Up to date. Checked ${fmt.ago(data.checkedAt)}.`
                      : 'Not checked yet.'}
            </Status>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={checkNow} disabled={checking}>
              {checking ? (
                <LoaderCircleIcon className="animate-spin" aria-hidden />
              ) : (
                <RefreshCwIcon aria-hidden />
              )}
              Check now
            </Button>
            {data?.available &&
              (data.canInstall ? (
                <Button
                  size="sm"
                  variant="primary"
                  loading={apply.isPending}
                  disabled={data.pending !== null}
                  onClick={() => apply.mutate()}
                >
                  <DownloadIcon aria-hidden />
                  {data.pending ? `${data.pending} is installing` : `Update to ${data.latest}`}
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="primary"
                  render={<a href={data.url} target="_blank" rel="noopener noreferrer" />}
                >
                  Open the release
                </Button>
              ))}
          </div>
        </div>
        {data?.problem && (
          <ErrorNotice error={new Error(data.problem)} title="The last check or update failed" />
        )}
        {apply.isError && <ErrorNotice error={apply.error} title="The update did not start" />}
      </div>
      <SettingRow
        title="Check for updates"
        description="Asks GitHub whether a newer version is out. The answer is kept, so restarting the app does not ask again."
        control={() => (
          <Select
            label="Check for updates"
            items={CHECK_EVERY}
            value={String(current.updateCheckHours)}
            className="w-40"
            onChange={(hours) =>
              change({ updateCheckHours: Number(hours) as Preferences['updateCheckHours'] })
            }
          />
        )}
      />
      <SettingRow
        title="Install updates automatically"
        description="When a newer version is out, Alpha Harness installs it and restarts by itself, but only once no simulations are running, so a run is never cut short. Off, the Update button in the sidebar waits for you."
        control={(labels) => (
          <Switch
            checked={current.autoUpdate}
            onChange={(on) => change({ autoUpdate: on })}
            {...labels}
          />
        )}
      >
        {data && !data.canInstall && data.isRelease && (
          <Status>
            This copy was not started by the Alpha Harness launcher, so it cannot install updates
            itself.
          </Status>
        )}
        {current.autoUpdate && current.updateCheckHours === 0 && (
          <Status>It only learns of a new version when you press Check now.</Status>
        )}
      </SettingRow>
    </>
  )
}

// ── Layout ───────────────────────────────────────────────────────────────────────────────

/** A tick on the chosen chips: pressed and unpressed alone read as on and disabled. */
function ticked<V extends string, T extends { value: V; label: string }>(items: T[], chosen: V[]) {
  return items.map((item) => ({
    ...item,
    label: chosen.includes(item.value) ? (
      <span className="flex items-center gap-1">
        <CheckIcon className="size-3.5" aria-hidden />
        {item.label}
      </span>
    ) : (
      item.label
    ),
  }))
}

function Status({ children }: { children: ReactNode }) {
  return <p className="text-body-compact text-ink-subtle">{children}</p>
}

function SettingRow({
  title,
  description,
  control,
  children,
  stacked,
}: {
  title: ReactNode
  description: ReactNode
  control: (labels: { 'aria-labelledby': string; 'aria-describedby': string }) => ReactNode
  /** What belongs to the row but sits under it: a status, or finer choices. */
  children?: ReactNode
  /** The control goes under the text, for one too wide to sit beside it. */
  stacked?: boolean
}) {
  const id = useId()
  const labels = { 'aria-labelledby': `${id}-title`, 'aria-describedby': `${id}-about` }
  return (
    <div className="flex flex-col gap-3 border-t border-hairline py-4">
      <div className={stacked ? 'flex flex-col gap-3' : 'flex items-start justify-between gap-6'}>
        <div className="flex min-w-0 flex-col gap-0.5">
          <span id={`${id}-title`} className="text-body font-medium text-ink">
            {title}
          </span>
          <p id={`${id}-about`} className="max-w-2xl text-body-compact text-ink-subtle">
            {description}
          </p>
        </div>
        <div className={stacked ? undefined : 'shrink-0 pt-0.5'}>{control(labels)}</div>
      </div>
      {children}
    </div>
  )
}
