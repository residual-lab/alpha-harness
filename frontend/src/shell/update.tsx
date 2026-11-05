/**
 * The Update affordance: nothing at all until a newer release is out, then one button in the
 * top bar. A consultant with ten minutes a day should never go looking for a version number.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DownloadIcon, LoaderCircleIcon, TriangleAlertIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { create } from 'zustand'
import { update } from '@/api/core'
import { errorMessage } from '@/api/http'
import { telemetry } from '@/lib/ws'
import { Button, ErrorNotice } from '@/ui/kit'
import { Dialog } from '@/ui/overlay'

/** Reads the backend's last answer; GitHub itself is asked only as often as Settings allow. */
const POLL_MS = 15 * 60 * 1000
/** Until the backend has looked once: it waits up to half a minute after starting first. */
const FIRST_LOOK_MS = 40_000
/** How often the overlay knocks while the backend is down being replaced. */
const RECONNECT_MS = 1500
/** Past this the install is stuck, and saying so beats a spinner that never resolves. */
const RECONNECT_GIVE_UP_MS = 5 * 60 * 1000

/**
 * Holds the screen from the moment the app is asked to close until the new one answers.
 *
 * Nothing else can: the backend stops mid-flight, so every open query and the socket fail at
 * once, and a page left as it was would show stale numbers behind a row of error toasts.
 */
function Installing({ version }: { version: string }) {
  const [waited, setWaited] = useState(0)

  useEffect(() => {
    const started = Date.now()
    // The old server is still answering for a second or so while it drains its lifespan. A
    // reload on the first 200 lands on a socket that is about to close, and the browser shows
    // its own connection-refused page, which nothing in the app can recover from. So a reload
    // needs proof the process turned over: either the version answering is the new one, or
    // this page watched the old one go away first.
    let wentAway = false
    const timer = setInterval(async () => {
      setWaited(Date.now() - started)
      try {
        // Not through `http`: its errors are written for a running backend, and this one is
        // expected to fail repeatedly while the launcher works.
        const response = await fetch('/api/health', { cache: 'no-store' })
        if (!response.ok) return
        const body = (await response.json()) as { version?: string }
        // Either proof will do. The second also covers a rollback, where what comes back is
        // the old version — the page reloads and the dialog explains why.
        if (body.version === version || wentAway) window.location.reload()
      } catch {
        wentAway = true
      }
    }, RECONNECT_MS)
    return () => clearInterval(timer)
  }, [version])

  const stuck = waited > RECONNECT_GIVE_UP_MS
  return (
    <div className="fixed inset-0 z-100 flex flex-col items-center justify-center gap-3 bg-canvas/95 p-6 text-center">
      <p className="text-title text-ink">Installing {version}</p>
      <p className="max-w-prose text-body text-ink-subtle">
        {stuck
          ? 'This is taking longer than it should. Close this window and start Alpha Harness again — the update is applied on the way back up.'
          : 'Alpha Harness is closing, installing the new version and starting again. This page reloads itself when it is ready.'}
      </p>
    </div>
  )
}

/** The version that served this page's code: the first one the backend names. */
let servedBy: string | null = null

// An install while this page is open (auto-install needs nobody there) restarts the backend
// under code built for the version before. Each time the socket connects, a different version
// answering means reload.
telemetry.onStatus((connected) => {
  if (!connected) return
  fetch('/api/health', { cache: 'no-store' })
    .then((response) => (response.ok ? response.json() : null))
    .then((body: { version?: string } | null) => {
      if (!body?.version) return
      servedBy ??= body.version
      if (body.version !== servedBy) window.location.reload()
    })
    .catch(() => {})
})

/** The version being installed, once Update was pressed anywhere, for the one overlay. */
const useInstalling = create<{ version: string | null; closed: boolean }>(() => ({
  version: null,
  closed: false,
}))

/** Holds the screen while an update installs, or once the app was quit. Mounted once. */
export function InstallingOverlay() {
  const version = useInstalling((s) => s.version)
  const closed = useInstalling((s) => s.closed)
  if (closed)
    return (
      <div className="fixed inset-0 z-100 flex flex-col items-center justify-center gap-3 bg-canvas/95 p-6 text-center">
        <p className="text-title text-ink">Alpha Harness is closed</p>
        <p className="max-w-prose text-body text-ink-subtle">
          Queued simulations wait until you open it again, the same way you opened it this time. You
          can close this tab.
        </p>
      </div>
    )
  return version === null ? null : <Installing version={version} />
}

/** Closes the app for good; the overlay says so, since every query is about to fail. */
export function useQuitApp() {
  return useMutation({
    mutationFn: update.quit,
    onSuccess: () => useInstalling.setState({ closed: true }),
  })
}

/** Hands the install to the launcher; the overlay takes the screen until the new one answers. */
export function useApplyUpdate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: update.apply,
    onSuccess: (started) => {
      // The backend is about to stop answering, so every other query is about to fail. Take
      // the screen before they do and wait for the new one, rather than leaving a dead page
      // behind a toast that says everything is fine.
      useInstalling.setState({ version: started.version })
      void queryClient.invalidateQueries({ queryKey: ['update'] })
    },
  })
}

/** One polling policy for the shared `['update']` query, so every reader agrees. */
export function useUpdateStatus() {
  return useQuery({
    queryKey: ['update'],
    queryFn: () => update.status(),
    staleTime: POLL_MS,
    refetchInterval: (query) => (query.state.data?.checkedAt ? POLL_MS : FIRST_LOOK_MS),
    // The desktop app spends most of its life hidden in the tray, and still has news to give.
    refetchIntervalInBackground: true,
    // A check that fails is not worth a retry storm: the next poll is the retry.
    retry: false,
  })
}

/**
 * The build, always on screen. Releases are dated, so this answers "is this from this morning
 * or from six months ago" in a screenshot or a support message without anyone having to go
 * looking. Visibility is the caller's to decide: it sits in the sidebar, which hides its
 * labels when collapsed to the rail.
 */
export function VersionBadge() {
  const queryClient = useQueryClient()
  const status = useUpdateStatus()
  const [rechecking, setRechecking] = useState(false)
  const current = status.data?.current
  // A check that could not reach GitHub leaves `problem` set and `available` false, so the
  // Update button never appears. Without this the app looks up to date when it simply does
  // not know — the one state it must not present as the other.
  const problem = status.data?.problem ?? (status.isError ? errorMessage(status.error) : null)

  const recheck = async () => {
    setRechecking(true)
    try {
      queryClient.setQueryData(['update'], await update.status(true))
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setRechecking(false)
    }
  }

  return (
    <span className="flex items-center gap-1.5 text-body-compact text-ink-subtle">
      <span className="num">{status.data?.isRelease ? current : 'dev'}</span>
      {problem && (
        <button
          type="button"
          onClick={recheck}
          disabled={rechecking}
          title={`${problem}\n\nClick to check again.`}
          aria-label={`Could not check for updates: ${problem}. Check again.`}
          className="inline-flex items-center rounded-xs text-status-warning transition-colors hover:text-ink disabled:text-ink-disabled"
        >
          {rechecking ? (
            <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <TriangleAlertIcon className="size-3.5" aria-hidden />
          )}
        </button>
      )}
    </span>
  )
}

/** The one thing the in-app updater cannot fix for you. */
function LauncherNotice({
  version,
  file,
  url,
  collapsed,
}: {
  version: string | null
  /** The release download for this machine, e.g. `AlphaHarness-macOS-arm64.dmg`. */
  file: string
  url: string
  collapsed: boolean
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      {collapsed ? (
        <Button
          size="icon-sm"
          variant="secondary"
          className="self-center"
          aria-label={`Update ${file}`}
          title={`A newer ${file} is out`}
          onClick={() => setOpen(true)}
        >
          <TriangleAlertIcon aria-hidden />
        </Button>
      ) : (
        <Button size="sm" variant="secondary" className="w-full" onClick={() => setOpen(true)}>
          <TriangleAlertIcon aria-hidden />
          Update {file}
        </Button>
      )}
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title={`A newer ${file} is out`}
        description={version ? `Yours is ${version}.` : 'Yours is from before they were dated.'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Not now
            </Button>
            <Button
              variant="primary"
              render={<a href={url} target="_blank" rel="noopener noreferrer" />}
            >
              Open the release
            </Button>
          </>
        }
      >
        <p className="text-body-compact text-ink-subtle">
          Alpha Harness updates itself, but it cannot replace the program that starts it. Download{' '}
          <span className="num">{file}</span> from the release, quit this one, then install and open
          the new one. Nothing you have is lost — your data, alphas and sign-in all stay. If you had
          the old <span className="num">AlphaHarness</span> program, or a shortcut you made to it,
          that can then go in the bin.
        </p>
      </Dialog>
    </>
  )
}

/** ``collapsed`` is the sidebar's icon rail, where there is room for a button but not a word. */
export function UpdateBadge({ collapsed = false }: { collapsed?: boolean }) {
  const [open, setOpen] = useState(false)
  const status = useUpdateStatus()
  const apply = useApplyUpdate()

  const data = status.data
  // An update installs the wheel and never the launcher, so a launcher change — the
  // system tray, say — reaches nobody until they fetch the exe themselves. Nothing else in
  // the app can say so: from inside, an out-of-date launcher looks exactly like a current one.
  // Beside the update, never instead of it: an older exe still installs one, and hiding the
  // button sent everyone with an early exe to GitHub for every release.
  const notice = data?.launcherOutdated ? (
    <LauncherNotice
      version={data.launcher}
      file={data.launcherFile}
      url={data.url || data.releasesUrl}
      collapsed={collapsed}
    />
  ) : null
  if (!data?.available) return notice

  return (
    <>
      {collapsed ? (
        <Button
          size="icon-sm"
          variant="primary"
          className="self-center"
          aria-label={`Update to ${data.latest}`}
          title={`Update to ${data.latest}`}
          onClick={() => setOpen(true)}
        >
          <DownloadIcon aria-hidden />
        </Button>
      ) : (
        <Button size="sm" variant="primary" className="w-full" onClick={() => setOpen(true)}>
          <DownloadIcon aria-hidden />
          Update to {data.latest}
        </Button>
      )}
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title={`Alpha Harness ${data.latest}`}
        description={`You are running ${data.current}.`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Not now
            </Button>
            {data.canInstall ? (
              <Button
                variant="primary"
                loading={apply.isPending}
                disabled={data.pending !== null}
                onClick={() => apply.mutate()}
              >
                {data.pending ? `${data.pending} is already installing` : 'Update and restart'}
              </Button>
            ) : (
              <Button
                variant="primary"
                render={<a href={data.url} target="_blank" rel="noopener noreferrer" />}
              >
                Open the release
              </Button>
            )}
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {/* The launcher's account of why the last attempt changed nothing. Without it the
              same Update button comes back after a failed install with no explanation. */}
          {data.problem && (
            <ErrorNotice error={new Error(data.problem)} title="The last update did not apply" />
          )}
          {apply.isError && <ErrorNotice error={apply.error} title="The update did not start" />}
          {!data.canInstall && (
            <p className="text-body-compact text-ink-subtle">
              This copy was not started by the Alpha Harness launcher, so it cannot update itself.
              Install the new version the way you installed this one.
            </p>
          )}
          {data.notes && (
            <pre className="max-h-72 overflow-auto rounded-sm border border-hairline bg-canvas p-3 text-body-compact whitespace-pre-wrap text-ink-muted">
              {data.notes}
            </pre>
          )}
          {data.canInstall && (
            <p className="text-body-compact text-ink-subtle">
              Alpha Harness closes, installs the new version and opens again. Simulations already
              running on BRAIN keep going and are picked up on the way back.
            </p>
          )}
        </div>
      </Dialog>
      {notice}
    </>
  )
}
