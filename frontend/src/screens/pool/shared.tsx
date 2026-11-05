/** Pieces the Alpha detail and Submittable tabs share: the expression, check figures, the BRAIN link, re-check. */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { CheckIcon, CopyIcon, EllipsisIcon, ExternalLinkIcon, RefreshCwIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/cn'
import { CORE_METRICS, fmt } from '@/lib/format'
import { BRAIN_ALPHA_URL, pool } from '@/screens/pool/api'
import { Button } from '@/ui/kit'
import { Menu } from '@/ui/overlay'
import { type AstKind, tokenizeBrainAst } from './brain-ast'

/** A check's value or limit: turnover as a percentage, the rest as a ratio. */
export const checkFigure = (name: string, v: number | null | undefined) =>
  name.includes('TURNOVER') ? CORE_METRICS.turnover.show(v) : fmt.ratio(v)

/** The strict submission boundary: submission happens on BRAIN, never here. */
export function OpenInBrain({ url }: { url: string }) {
  return (
    <Button size="sm" render={<a href={url} target="_blank" rel="noopener noreferrer" />}>
      <ExternalLinkIcon aria-hidden />
      Open in BRAIN
    </Button>
  )
}

/** Re-runs BRAIN's submission checks (never submits) and refreshes what depends on them. */
export function RecheckButton({
  alphaId,
  label = 'Re-check on BRAIN',
  disabled,
}: {
  alphaId: string
  label?: string
  disabled?: boolean
}) {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => pool.check(alphaId),
    onSuccess: (body) => {
      const checks = body.is?.checks ?? []
      const count = (r: string) => checks.filter((c) => (c.result ?? 'PENDING') === r).length
      toast.success(
        `Checked ${alphaId}: ${count('PASS')} pass, ${count('FAIL')} fail, ${count('PENDING')} pending`,
      )
      void queryClient.invalidateQueries({
        queryKey: ['pool', 'detail', alphaId],
      })
      // The Alpha page holds the verdict and checks under its own key.
      void queryClient.invalidateQueries({ queryKey: ['alpha', alphaId, 'page'] })
      void queryClient.invalidateQueries({ queryKey: ['pool', 'submittable'] })
      // Not under the prefix above: every market's count, which an empty market points to.
      void queryClient.invalidateQueries({ queryKey: ['pool', 'submittable-count'] })
      // The task tables judge on the same checks.
      void queryClient.invalidateQueries({ queryKey: ['lab-tasks'] })
      void queryClient.invalidateQueries({ queryKey: ['lab-task-top'] })
      void queryClient.invalidateQueries({ queryKey: ['submittable-alphas'] })
    },
  })
  return (
    <Button
      size="sm"
      loading={mutation.isPending}
      disabled={disabled}
      onClick={() => mutation.mutate()}
    >
      {!mutation.isPending && <RefreshCwIcon />}
      {label}
    </Button>
  )
}

/**
 * Per-Alpha actions that are not one-click enough to earn a button of their own. `links` adds
 * the full page and BRAIN, for a table row, where nothing else on the row offers them.
 */
export function AlphaActionsMenu({ alphaId, links = false }: { alphaId: string; links?: boolean }) {
  const navigate = useNavigate()
  return (
    <Menu
      trigger={
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={`More actions for ${alphaId}`}
          // In the Stored table this button sits inside the row's click target, which would
          // otherwise open the detail sheet behind the menu.
          onClick={(e) => e.stopPropagation()}
        >
          <EllipsisIcon />
        </Button>
      }
      items={[
        ...(links
          ? [
              {
                label: 'Open Full Page',
                onClick: () => void navigate({ to: '/alpha/$alphaId', params: { alphaId } }),
              },
              {
                label: 'Open in BRAIN',
                onClick: () =>
                  window.open(BRAIN_ALPHA_URL(alphaId), '_blank', 'noopener,noreferrer'),
              },
            ]
          : []),
        {
          label: 'Settings Sampler',
          onClick: () =>
            void navigate({ to: '/tools/settings-sampler', search: { alpha: alphaId } }),
        },
        {
          label: 'Fields Sampler',
          onClick: () => void navigate({ to: '/tools/fields-sampler', search: { alpha: alphaId } }),
        },
        {
          label: 'Correlation Breaker',
          onClick: () =>
            void navigate({ to: '/tools/correlation-breaker', search: { alpha: alphaId } }),
        },
      ]}
    />
  )
}

const AST_CLASS: Record<AstKind, string | undefined> = {
  operator: 'font-medium text-link',
  number: 'text-ink-muted',
  punctuation: 'text-ink-subtle',
  field: 'text-ink',
  other: undefined,
}

/** The Alpha's Fast Expression, syntax-coloured and copyable (DESIGN.md ast-editor). */
export function AstInspector({
  expression,
  className,
  allowCopy = true,
}: {
  expression: string | null | undefined
  className?: string
  allowCopy?: boolean
}) {
  const [copied, setCopied] = useState(false)

  if (!expression) {
    return <span className="num text-body-compact text-ink-subtle">—</span>
  }

  const copy = () => {
    navigator.clipboard.writeText(expression).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
      },
      () => {},
    )
  }

  const tokens = tokenizeBrainAst(expression)

  return (
    <div
      className={cn(
        'group relative flex min-w-0 items-start justify-between gap-2 rounded-sm border border-hairline bg-canvas px-3 py-2 transition-colors hover:border-hairline-strong',
        className,
      )}
    >
      {/* `pre-wrap`, not the browser's default for `code`, which collapses every run of
          whitespace: a multi-statement expression is written across lines and reads as one
          line without it. Still wraps, so a long single line is not pushed off the panel. */}
      <code className="num block min-w-0 flex-1 break-all whitespace-pre-wrap text-body-compact leading-relaxed text-ink selection:bg-primary-subtle">
        {tokens.map((token, i) => (
          <span key={i} className={AST_CLASS[token.kind]}>
            {token.text}
          </span>
        ))}
      </code>
      {allowCopy && (
        <button
          type="button"
          onClick={copy}
          title={copied ? 'Copied to clipboard' : 'Copy expression'}
          aria-label={copied ? 'Copied to clipboard' : 'Copy expression'}
          className="flex size-6 shrink-0 items-center justify-center rounded-xs text-ink-subtle opacity-70 transition-all hover:bg-surface-2 hover:text-ink hover:opacity-100 group-hover:opacity-100"
        >
          {copied ? (
            <CheckIcon className="size-3.5 text-pnl-positive-text" />
          ) : (
            <CopyIcon className="size-3.5" />
          )}
        </button>
      )}
    </div>
  )
}
