/**
 * The one gate. Nothing works without a BRAIN session.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldCheckIcon } from 'lucide-react'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { auth } from '@/api/core'
import { ApiError } from '@/api/http'
import { useLive } from '@/lib/live'
import { Button, ErrorNotice, Field, Input, Notice } from '@/ui/kit'

/** How often the app asks BRAIN whether the identity check has been accepted. */
const VERIFY_EVERY_MS = 2_500

/** Larger than the in-app controls: this screen is the whole page and is read from arm's length. */
const FIELD = '[&>span:first-child]:text-body'
const INPUT = 'h-11 px-4 text-title'

/** A missing entry, said under its own field instead of in the notice. */
class FieldError extends Error {
  field: 'email' | 'password'
  constructor(field: 'email' | 'password', message: string) {
    super(message)
    this.field = field
  }
}

export function SignIn({ storedEmail }: { storedEmail: string | null }) {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState(storedEmail ?? '')
  const [password, setPassword] = useState('')
  const [shown, setShown] = useState(false)

  const signIn = useMutation({
    meta: { inline: true },
    mutationFn: async () => {
      const trimmed = email.trim()
      const reuseStored = !password && storedEmail && (!trimmed || trimmed === storedEmail)
      if (!trimmed && !storedEmail) throw new FieldError('email', 'Enter your BRAIN account email.')
      if (!password && !reuseStored) throw new FieldError('password', 'Enter your password.')
      const session = reuseStored ? await auth.login() : await auth.login(trimmed, password)
      if (!session.authenticated) {
        throw new ApiError(401, {
          code: session.verificationUrl ? 'verification_required' : 'not_authenticated',
          message: session.detail || 'That email and password were not accepted.',
          ...(session.verificationUrl ? { verificationUrl: session.verificationUrl } : {}),
          ...(session.inquiry ? { inquiry: session.inquiry } : {}),
        })
      }
      return session
    },
    onSuccess: () => {
      setPassword('')
      useLive.setState({ verificationUrl: null })
      void queryClient.invalidateQueries()
    },
  })

  const failed = signIn.error instanceof ApiError ? signIn.error : null
  const verificationUrl =
    typeof failed?.body.verificationUrl === 'string' ? failed.body.verificationUrl : null
  const inquiry = typeof failed?.body['inquiry'] === 'string' ? failed.body['inquiry'] : null
  const missing = signIn.error instanceof FieldError ? signIn.error : null

  const done = () => {
    setPassword('')
    useLive.setState({ verificationUrl: null })
    signIn.reset()
    void queryClient.invalidateQueries()
  }

  return (
    <div className="flex min-h-svh flex-col items-center justify-center p-6">
      <div className="flex w-full max-w-120 flex-col gap-8">
        <div className="flex items-center gap-3 self-center">
          <img src="/mark.svg" alt="" className="size-10" />
          <span className="text-headline font-semibold text-ink">Alpha Harness</span>
        </div>

        <form
          autoComplete="off"
          className="panel-highlight flex flex-col gap-5 rounded-lg border border-hairline bg-surface-1 p-10"
          onSubmit={(event: FormEvent) => {
            event.preventDefault()
            signIn.mutate()
          }}
        >
          <div className="mb-2 flex flex-col gap-2 text-center">
            <h1 className="text-headline">Sign in to BRAIN</h1>
            <p className="text-title text-ink-subtle">Use your WorldQuant BRAIN account.</p>
          </div>

          <Field
            label="Email"
            className={FIELD}
            error={missing?.field === 'email' && missing.message}
          >
            <Input
              type="email"
              autoComplete="email"
              placeholder="you@example.com"
              required={!storedEmail}
              value={email}
              disabled={signIn.isPending}
              aria-invalid={missing?.field === 'email'}
              className={INPUT}
              onChange={(event) => {
                signIn.reset()
                setEmail(event.target.value)
              }}
            />
          </Field>
          <Field
            // Named by this span alone: the Show button inside the label would otherwise join the name.
            label={<span id="brain-password-name">Password</span>}
            className={FIELD}
            error={missing?.field === 'password' && missing.message}
          >
            <div className="relative">
              <Input
                id="brain-password"
                aria-labelledby="brain-password-name"
                type={shown ? 'text' : 'password'}
                autoComplete="current-password"
                placeholder={
                  storedEmail ? 'Leave blank to use the saved password' : 'Your BRAIN password'
                }
                value={password}
                disabled={signIn.isPending}
                aria-invalid={missing?.field === 'password'}
                className={`${INPUT} pr-18`}
                onChange={(event) => {
                  signIn.reset()
                  setPassword(event.target.value)
                }}
              />
              <button
                type="button"
                aria-pressed={shown}
                aria-controls="brain-password"
                disabled={signIn.isPending}
                onClick={() => setShown((s) => !s)}
                className="absolute inset-y-0 right-3 my-auto h-8 rounded-sm px-2 text-body text-ink-subtle transition-colors hover:bg-surface-2 hover:text-ink disabled:text-ink-disabled"
              >
                {shown ? 'Hide' : 'Show'}
              </button>
            </div>
          </Field>

          {verificationUrl && inquiry ? (
            <Verification url={verificationUrl} inquiry={inquiry} onVerified={done} />
          ) : (
            signIn.isError &&
            !missing && <ErrorNotice error={signIn.error} title="Sign-in failed" />
          )}

          <Button
            type="submit"
            variant="primary"
            loading={signIn.isPending}
            className="mt-1 h-11 text-title"
          >
            {signIn.isPending ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>
      </div>
    </div>
  )
}

/** BRAIN's identity check, waited on rather than handed over as a link.
 *
 * The check itself cannot live in this page: BRAIN answers `X-Frame-Options: DENY`, and the
 * Persona widget behind it frames only into WorldQuant's own domain. So it opens in a window
 * of its own, and this asks BRAIN every couple of seconds whether it has been accepted —
 * which is what lets the sign-in finish by itself rather than asking for the password again.
 */
function Verification({
  url,
  inquiry,
  onVerified,
}: {
  url: string
  inquiry: string
  onVerified: () => void
}) {
  const [waiting, setWaiting] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const popup = useRef<Window | null>(null)

  useEffect(() => {
    if (!waiting) return
    let stopped = false
    const timer = setInterval(async () => {
      try {
        const session = await auth.verify(inquiry)
        if (stopped) return
        if (session.authenticated) {
          stopped = true
          clearInterval(timer)
          popup.current?.close()
          onVerified()
        }
      } catch (error) {
        // Asking early is normal and answers unauthenticated; a real failure stops the wait
        // rather than polling BRAIN forever.
        stopped = true
        clearInterval(timer)
        setWaiting(false)
        setProblem(error instanceof ApiError ? error.message : 'The check could not be read.')
      }
    }, VERIFY_EVERY_MS)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [waiting, inquiry, onVerified])

  const open = () => {
    setProblem(null)
    setWaiting(true)
    // Named, so a second click returns to the window already open rather than making another.
    popup.current = window.open(url, 'brain-identity-check', 'width=520,height=760')
    if (!popup.current) {
      setWaiting(false)
      setProblem(
        'Your browser blocked the window. Allow pop-ups for this page, or open the check in a new tab.',
      )
    }
  }

  return (
    <Notice tone="warn" title="BRAIN needs to verify your identity">
      <div className="flex flex-col gap-3">
        <p>
          {waiting
            ? 'Finish the check in the window that opened. This page signs you in as soon as BRAIN accepts it — leave it open.'
            : 'This happens once. The check opens in its own window; you will not need to type your password again.'}
        </p>
        {problem && <p className="text-status-warning">{problem}</p>}
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="primary" onClick={open} loading={waiting}>
            <ShieldCheckIcon />
            {waiting ? 'Waiting for BRAIN…' : 'Verify identity'}
          </Button>
          {waiting && (
            <button
              type="button"
              className="text-body text-ink-subtle underline-offset-2 hover:underline"
              onClick={() => setWaiting(false)}
            >
              Stop waiting
            </button>
          )}
        </div>
      </div>
    </Notice>
  )
}
