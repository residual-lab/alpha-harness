/** Name, category, colour, tags and description, saved to the Alpha on BRAIN. Saving never submits. */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useBlocker } from '@tanstack/react-router'
import { CheckIcon, CopyIcon, SparklesIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { POWER_POOL_DESCRIPTION_MIN_LENGTH } from '@/api/platform'
import { cn } from '@/lib/cn'
import { usePreferences } from '@/lib/preferences'
import { useRunnableModels } from '@/screens/ai/shared'
import { Button, ErrorNotice, Field, Input, Panel, Textarea } from '@/ui/kit'
import { Confirm, Select } from '@/ui/overlay'
import { type AlphaInfo, type AlphaView, alpha as api } from './api'
import { describedForPowerPool, descriptionProblems, POWER_POOL_TEMPLATE, stamp } from './brain'

const CATEGORIES = [
  'NONE',
  'PRICE_REVERSION',
  'PRICE_MOMENTUM',
  'VOLUME',
  'FUNDAMENTAL',
  'ANALYST',
  'PRICE_VOLUME',
  'RELATION',
  'SENTIMENT',
] as const

const readable = (value: string) =>
  value === 'NONE' ? 'None' : value.charAt(0) + value.slice(1).toLowerCase().replaceAll('_', ' ')

/** BRAIN's five colours, as it names them. */
const COLORS: { value: string; swatch: string }[] = [
  { value: 'RED', swatch: 'bg-(--red-400)' },
  { value: 'YELLOW', swatch: 'bg-(--amber-300)' },
  { value: 'GREEN', swatch: 'bg-(--sea-400)' },
  { value: 'BLUE', swatch: 'bg-(--teal-400)' },
  { value: 'PURPLE', swatch: 'bg-(--orchid-400)' },
]

interface Draft {
  name: string
  category: string
  color: string
  tags: string
  description: string
}

const draftOf = (a: AlphaInfo): Draft => ({
  name: a.name ?? '',
  category: a.category ?? 'NONE',
  color: a.color ?? 'NONE',
  tags: a.tags.join(', '),
  description: a.description ?? '',
})

function AlphaId({ alphaId }: { alphaId: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <p className="flex items-center gap-1.5 text-body">
      <span className="font-medium text-ink">Alpha ID:</span>
      <span className="num text-ink-muted">{alphaId}</span>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={`Copy ${alphaId}`}
        onClick={() =>
          navigator.clipboard.writeText(alphaId).then(
            () => {
              setCopied(true)
              setTimeout(() => setCopied(false), 2000)
            },
            () => toast.error('The browser did not allow copying'),
          )
        }
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </Button>
    </p>
  )
}

/** Why Write with AI cannot run: null when it can, undefined while that is still being read.
 *  It needs a model one of the Keys can run. */
function useWriterBlocked(): string | null | undefined {
  const { runnable, all, ready } = useRunnableModels()
  const preferences = usePreferences()
  if (!ready || preferences.isPending) return undefined
  const chosen = preferences.data?.descriptionModel
  if (runnable.length === 0)
    return 'Write with AI is off: none of your Keys can run a model. Add a Key, and set up a model for it, under LLM Integration.'
  if (chosen && !all.some((m) => m.ref === chosen))
    return 'Write with AI is off: the model chosen in Settings › Alphas is no longer set up. Choose another one there.'
  if (chosen && !runnable.some((m) => m.ref === chosen))
    return 'Write with AI is off: the model chosen in Settings › Alphas has no enabled Key. Enable one under LLM Integration, or choose another model.'
  return null
}

export function PropertiesPanel({ alpha }: { alpha: AlphaInfo }) {
  const queryClient = useQueryClient()
  const blocked = useWriterBlocked()
  const saved = draftOf(alpha)
  const [draft, setDraft] = useState<Draft>(saved)
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved)
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft({ ...draft, [key]: value })
  const powerPool = describedForPowerPool(alpha)
  const problems =
    powerPool && draft.description !== saved.description
      ? descriptionProblems(draft.description)
      : []

  const save = useMutation({
    meta: { inline: true },
    mutationFn: async () => {
      // BRAIN drops an empty description from the request rather than clearing it, so refuse
      // the save instead of reporting one that never happened.
      if (saved.description.trim() && !draft.description.trim()) {
        throw new Error(
          'BRAIN keeps a description once it is set and cannot clear it. Write a new one ' +
            'instead. Nothing was saved.',
        )
      }
      return api.save(alpha.alphaId, {
        name: draft.name,
        category: draft.category,
        color: draft.color,
        tags: draft.tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
        description: draft.description,
      })
    },
    onSuccess: (info) => {
      queryClient.setQueryData<AlphaView>(['alpha', alpha.alphaId, 'page'], (page) =>
        page ? { ...page, alpha: info } : page,
      )
      setDraft(draftOf(info))
      toast.success(`Saved the properties of ${alpha.alphaId}`)
    },
  })

  const write = useMutation({
    meta: { inline: true },
    mutationFn: () => api.describe(alpha.alphaId),
    onSuccess: (written) => {
      setDraft((d) => ({ ...d, description: written.description }))
      toast.success(`Written by ${written.model}`, {
        description: 'Read it through, then Save to BRAIN.',
      })
    },
  })
  // A description written by hand, or by a model on a paid request, is not dropped unasked.
  const leaving = useBlocker({
    shouldBlockFn: () => dirty && !save.isPending,
    enableBeforeUnload: () => dirty,
    withResolver: true,
  })

  return (
    <Panel
      title="Properties"
      description={save.isPending ? 'Saving...' : `Last saved ${stamp(alpha.dateModified)}`}
      actions={
        <>
          {dirty && (
            <Button size="sm" variant="ghost" onClick={() => setDraft(saved)}>
              Discard
            </Button>
          )}
          <Button
            size="sm"
            variant="primary"
            disabled={!dirty}
            loading={save.isPending}
            onClick={() => save.mutate()}
          >
            Save to BRAIN
          </Button>
        </>
      }
      bodyClassName="flex flex-col gap-4"
    >
      {save.isError && <ErrorNotice error={save.error} title="The properties were not saved" />}
      {write.isError && <ErrorNotice error={write.error} title="The description was not written" />}
      <AlphaId alphaId={alpha.alphaId} />
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name">
          <Input
            value={draft.name}
            placeholder={`Currently '${alpha.name ?? 'anonymous'}'`}
            onChange={(e) => set('name', e.target.value)}
          />
        </Field>
        <Field label="Category">
          <Select
            label="Category"
            value={draft.category}
            onChange={(v) => set('category', v)}
            items={CATEGORIES.map((c) => ({ value: c, label: readable(c) }))}
          />
        </Field>
        <Field label="Tags" hint="Separate tags with commas">
          <Input
            value={draft.tags}
            placeholder="Select/add tags"
            onChange={(e) => set('tags', e.target.value)}
          />
        </Field>
        <fieldset className="flex min-w-0 flex-col gap-1.5">
          <legend className="float-left w-full text-caption font-medium text-ink-muted">
            Color
          </legend>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              aria-pressed={draft.color === 'NONE'}
              onClick={() => set('color', 'NONE')}
              className={cn(
                'h-7 rounded-sm border px-2 text-body-compact transition-colors',
                draft.color === 'NONE'
                  ? 'border-ink-subtle bg-surface-3 text-ink'
                  : 'border-hairline-strong text-ink-subtle hover:text-ink',
              )}
            >
              None
            </button>
            {COLORS.map((c) => (
              <button
                key={c.value}
                type="button"
                aria-label={readable(c.value)}
                aria-pressed={draft.color === c.value}
                onClick={() => set('color', c.value)}
                className={cn(
                  'size-7 rounded-sm ring-offset-2 ring-offset-surface-1 transition-shadow',
                  c.swatch,
                  draft.color === c.value
                    ? 'ring-2 ring-ink'
                    : 'hover:ring-1 hover:ring-ink-subtle',
                )}
              />
            ))}
          </div>
        </fieldset>
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-caption font-medium text-ink-muted">Description</span>
          {powerPool && (
            <span className="num text-body-compact text-ink-subtle">
              {draft.description.trim().length} / {POWER_POOL_DESCRIPTION_MIN_LENGTH} character
              minimum
            </span>
          )}
        </div>
        {powerPool && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-hairline-strong bg-surface-2 px-3 py-2">
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="text-body font-medium text-ink">Power Pool Alpha</span>
              <span className="text-body-compact text-pretty text-ink-subtle">
                In order to submit this alpha, you have to add an alpha description following the
                given template.
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                loading={write.isPending}
                disabled={blocked !== null}
                title="The model chosen in Settings › Alphas writes it from this Alpha's expression, fields and operators"
                onClick={() => write.mutate()}
              >
                {!write.isPending && <SparklesIcon aria-hidden />}
                {draft.description.trim() ? 'Rewrite with AI' : 'Write with AI'}
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={() => set('description', POWER_POOL_TEMPLATE)}
              >
                Use the template
              </Button>
            </div>
            {/* A disabled button shows no tooltip, so the reason is said here. */}
            {blocked && (
              <p className="w-full text-body-compact text-pretty text-ink-subtle">{blocked}</p>
            )}
          </div>
        )}
        <Textarea
          rows={3}
          aria-label="Description"
          aria-invalid={problems.length > 0 || undefined}
          // Held while the model writes, or what was typed meanwhile would be replaced.
          readOnly={write.isPending}
          value={draft.description}
          onChange={(e) => set('description', e.target.value)}
          placeholder="Description"
        />
        {problems.map((p) => (
          <p key={p} className="text-body-compact text-pnl-negative-text">
            {p}
          </p>
        ))}
      </div>
      <Confirm
        open={leaving.status === 'blocked'}
        onOpenChange={(open) => !open && leaving.reset?.()}
        title="Leave without saving?"
        confirmLabel="Leave"
        cancelLabel="Stay"
        danger
        onConfirm={() => leaving.proceed?.()}
      >
        The changes to this Alpha's properties are not saved to BRAIN yet.
      </Confirm>
    </Panel>
  )
}
