import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import './index.css'
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Toaster, toast } from 'sonner'
import { ApiError, errorMessage } from '@/api/http'
import { useLive } from '@/lib/live'
import { telemetry } from '@/lib/ws'
import { router } from '@/router'
import { useDatasetPick, useFieldPick } from '@/screens/data/dataset-pick'
import { endDatasetPick, startDatasetPick, useFieldFilter } from '@/screens/data/state'
import { useSeedPick } from '@/screens/research-labs/evolution/seed-pick'

/** Any call BRAIN answers with a verification demand raises the global banner. */
const noteVerification = (error: unknown) => {
  if (error instanceof ApiError && error.body.verificationUrl)
    useLive.setState({ verificationUrl: error.body.verificationUrl })
}

const queryClient = new QueryClient({
  queryCache: new QueryCache({ onError: noteVerification }),
  mutationCache: new MutationCache({
    onError: (error, _variables, _result, mutation) => {
      noteVerification(error)
      // A mutation with its own onError, or one that shows its error inline, says it itself.
      if (!mutation.options.onError && !mutation.meta?.['inline']) toast.error(errorMessage(error))
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
      // The backend says whether a failure is worth repeating: a 429 from BRAIN's throttle
      // is, the daily limit is not.
      retry: (count, error) => {
        if (count >= 2) return false
        if (error instanceof ApiError) return error.status === 0 || error.retryable
        return true
      },
    },
  },
})

const root = document.getElementById('root')
if (!root) throw new Error('index.html has no #root element')

telemetry.connect()

// A pick left for anywhere but a detour its screen links to is cancelled, so that screen
// does not stay a picker for a lab the user walked away from.
const within = (path: string, places: string[]) =>
  places.some((place) => path === place || path.startsWith(`${place}/`))
// What was ticked is offered back once, so a wrong click costs nothing.
router.subscribe('onResolved', ({ toLocation: { pathname } }) => {
  const datasets = useDatasetPick.getState()
  if (datasets.active && !within(pathname, ['/data', '/sync', '/pyramids'])) {
    const { scope, ids, byMarket, from } = datasets
    const { filter } = useFieldFilter.getState()
    const { fields } = useFieldPick.getState()
    endDatasetPick(false)
    if (scope && ids.length)
      toast('Dataset pick closed', {
        description: `${ids.length} dataset${ids.length === 1 ? '' : 's'} ticked, not yet used.`,
        action: {
          label: 'Resume',
          onClick: () => {
            startDatasetPick(scope, ids, from, filter)
            useDatasetPick.setState({ byMarket })
            useFieldPick.setState({ fields })
            void router.navigate({ to: '/data' })
          },
        },
      })
  }
  const seeds = useSeedPick.getState()
  if (seeds.active && !within(pathname, ['/pool', '/alpha'])) {
    const { scope, ids, from } = seeds
    seeds.cancel()
    if (scope && ids.length)
      toast('Seed pick closed', {
        description: `${ids.length} seed${ids.length === 1 ? '' : 's'} ticked, not yet used.`,
        action: {
          label: 'Resume',
          onClick: () => {
            useSeedPick.getState().start(scope, ids, from)
            void router.navigate({ to: '/pool/$tab', params: { tab: 'stored' } })
          },
        },
      })
  }
})

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster
        theme="dark"
        position="bottom-right"
        toastOptions={{
          style: {
            background: 'var(--color-surface-3)',
            border: '1px solid var(--color-hairline-strong)',
            color: 'var(--color-ink)',
            borderRadius: 8,
            boxShadow: 'var(--shadow-float)',
          },
        }}
      />
    </QueryClientProvider>
  </StrictMode>,
)
