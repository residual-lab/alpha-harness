/**
 * LLM Prompts: write system prompts, copy the built-ins, and edit them in place. Every edit
 * saves itself as it is typed, and a task using the prompt sends the new text on its next call.
 */

import { type QueryClient, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { CopyIcon, LockIcon, PlusIcon, ScrollTextIcon, Trash2Icon } from 'lucide-react'
import { useCallback, useDeferredValue, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/api/http'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import {
  Button,
  Empty,
  ErrorNotice,
  Field,
  Input,
  KV,
  Notice,
  Page,
  PageHeader,
  Panel,
  Segmented,
  Skeleton,
  Textarea,
} from '@/ui/kit'
import { Confirm, Menu } from '@/ui/overlay'
import { SplitPane } from '@/ui/panels'
import {
  countText,
  LIBRARY_KEY,
  type LibraryPrompt,
  type NewPrompt,
  type PromptKind,
  type PromptLibrary,
  promptKey,
  prompts,
  usePromptLibrary,
} from './api'
import { diffCounts, diffWords } from './diff'

const TEXT =
  'num min-h-[60vh] resize-y text-body-compact leading-relaxed whitespace-pre-wrap text-ink'

/** Change one saved prompt in the cached library, so the list and every picker agree at once. */
function patchLibrary(queryClient: QueryClient, id: number, patch: Partial<LibraryPrompt>) {
  queryClient.setQueryData<PromptLibrary>(
    LIBRARY_KEY,
    (lib) =>
      lib && { ...lib, prompts: lib.prompts.map((p) => (p.id === id ? { ...p, ...patch } : p)) },
  )
}

/** The lab previews show the prompt's text; they are keyed by the request, not by it. */
const refreshPreviews = (queryClient: QueryClient) =>
  queryClient.invalidateQueries({ queryKey: ['power-pool-lab', 'preview'] })

export function PromptsScreen() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { open } = useSearch({ from: '/prompts' })
  const library = usePromptLibrary()
  const all = library.data?.prompts ?? []
  const kinds = library.data?.kinds ?? []
  const current = all.find((p) => promptKey(p) === String(open)) ?? all[0]

  const select = useCallback(
    (key: string) =>
      void navigate({
        to: '/prompts',
        search: { open: /^\d+$/.test(key) ? Number(key) : key },
        replace: true,
      }),
    [navigate],
  )

  const create = useMutation({
    mutationFn: (body: NewPrompt) => prompts.create(body),
    onSuccess: (row) => {
      queryClient.setQueryData<PromptLibrary>(LIBRARY_KEY, (lib) => {
        if (!lib) return lib
        const builtIns = lib.prompts.filter((p) => p.builtIn)
        const saved = lib.prompts.filter((p) => !p.builtIn)
        return { ...lib, prompts: [...builtIns, row, ...saved] }
      })
      select(promptKey(row))
      toast.success(`Saved “${row.name}”`)
    },
  })

  return (
    <Page>
      <PageHeader
        title="LLM Prompts"
        description="The instructions a model gets before anything else. Built-in prompts stay as they are; save a copy to change one, then choose it next to the datasets in LLM Power Pool Lab, or in the Assistant."
        actions={
          <Menu
            trigger={
              <Button variant="primary" loading={create.isPending} disabled={kinds.length === 0}>
                <PlusIcon />
                New Prompt
              </Button>
            }
            items={kinds.map((k) => ({
              label: `For ${k.label}`,
              onClick: () =>
                create.mutate({ name: `New ${k.label} prompt`, kind: k.slug, body: '' }),
            }))}
          />
        }
      />
      {library.isError && <ErrorNotice title="Could not load the prompts" error={library.error} />}
      {create.isError && <ErrorNotice title="Could not save the prompt" error={create.error} />}
      {!library.data ? (
        !library.isError && <Skeleton className="h-96" />
      ) : (
        <SplitPane id="llm-prompts" first={{ default: 300, min: 220, max: 460 }}>
          <Panel title="Library" bodyClassName="p-2">
            <PromptList kinds={kinds} prompts={all} current={current} onSelect={select} />
          </Panel>
          {current ? (
            <Editor
              key={promptKey(current)}
              prompt={current}
              kind={kinds.find((k) => k.slug === current.kind)}
              original={all.find((p) => p.builtIn && p.kind === current.kind)}
              copying={create.isPending}
              onCopy={(body) => create.mutate(body)}
              onDeleted={() => select(current.kind)}
            />
          ) : (
            <Panel>
              <Empty title="No prompts" icon={<ScrollTextIcon />} />
            </Panel>
          )}
        </SplitPane>
      )}
    </Page>
  )
}

function PromptList({
  kinds,
  prompts: all,
  current,
  onSelect,
}: {
  kinds: PromptKind[]
  prompts: LibraryPrompt[]
  current: LibraryPrompt | undefined
  onSelect: (key: string) => void
}) {
  return (
    <div className="flex max-h-[75vh] flex-col gap-3 overflow-auto">
      {kinds.map((k) => (
        <section key={k.slug} aria-label={k.label} className="flex flex-col gap-0.5">
          <h3 className="px-2 pt-1 pb-0.5 text-caption font-medium text-ink-subtle">{k.label}</h3>
          <ul className="flex flex-col gap-0.5">
            {all
              .filter((p) => p.kind === k.slug)
              .map((p) => {
                const active = current !== undefined && promptKey(p) === promptKey(current)
                return (
                  <li key={promptKey(p)}>
                    <button
                      type="button"
                      onClick={() => onSelect(promptKey(p))}
                      aria-current={active ? 'true' : undefined}
                      className={cn(
                        'flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-surface-2',
                        active && 'bg-surface-2',
                      )}
                    >
                      <span
                        className={cn(
                          'flex items-center gap-1.5 truncate text-body',
                          active ? 'text-ink' : 'text-ink-muted',
                        )}
                      >
                        {p.builtIn && <LockIcon className="size-3 shrink-0 text-ink-subtle" />}
                        <span className="truncate">{p.name || 'Untitled'}</span>
                      </span>
                      <span className="num truncate text-body-compact text-ink-subtle">
                        {p.builtIn ? 'Built-in' : fmt.ago(p.updatedAt)} · {fmt.int(p.words)} words
                      </span>
                    </button>
                  </li>
                )
              })}
          </ul>
        </section>
      ))}
    </div>
  )
}

type Change = { name?: string; body?: string }
type SaveStatus = 'idle' | 'saving' | 'saved' | 'error'

/**
 * Saves a prompt's edits as they are typed: the cache changes on every keystroke, the backend
 * a moment after typing pauses. Saves go out one at a time and in order, so a slow one can never
 * land after a newer one, and whatever is still waiting is sent when the editor closes.
 */
function useAutosave(id: number | null) {
  const queryClient = useQueryClient()
  const pending = useRef<Change>({})
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const chain = useRef<Promise<unknown>>(Promise.resolve())
  const inFlight = useRef(0)
  const alive = useRef(true)
  const [status, setStatus] = useState<SaveStatus>('idle')
  const [error, setError] = useState<unknown>(null)

  const flush = useCallback(() => {
    clearTimeout(timer.current)
    const change = pending.current
    pending.current = {}
    if (id === null || Object.keys(change).length === 0) return
    inFlight.current++
    chain.current = chain.current
      .then(() => prompts.edit(id, change))
      .then(
        (row) => {
          patchLibrary(queryClient, id, { updatedAt: row.updatedAt })
          void refreshPreviews(queryClient)
          if (alive.current && inFlight.current === 1 && !Object.keys(pending.current).length)
            setStatus('saved')
        },
        (e: unknown) => {
          // Kept, so the next keystroke or Retry sends it again.
          pending.current = { ...change, ...pending.current }
          if (alive.current) {
            setError(e)
            setStatus('error')
          }
        },
      )
      .finally(() => inFlight.current--)
  }, [id, queryClient])

  const queue = useCallback(
    (change: Change) => {
      if (id === null) return
      pending.current = { ...pending.current, ...change }
      patchLibrary(queryClient, id, {
        ...change,
        ...(change.body !== undefined && countText(change.body)),
      })
      setStatus('saving')
      clearTimeout(timer.current)
      timer.current = setTimeout(flush, 400)
    },
    [id, queryClient, flush],
  )

  const discard = useCallback(() => {
    clearTimeout(timer.current)
    pending.current = {}
  }, [])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      flush()
    }
  }, [flush])

  return { status, error, queue, flush, discard }
}

function SaveState({
  status,
  error,
  onRetry,
}: {
  status: SaveStatus
  error: unknown
  onRetry: () => void
}) {
  if (status === 'saving') return <span className="text-body-compact text-ink-subtle">Saving…</span>
  if (status === 'saved') return <span className="text-body-compact text-ink-subtle">Saved</span>
  if (status === 'error')
    return (
      <span className="flex items-center gap-2 text-body-compact text-pnl-negative" role="alert">
        Not saved: {errorMessage(error)}
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      </span>
    )
  return null
}

function Editor({
  prompt,
  kind,
  original,
  copying,
  onCopy,
  onDeleted,
}: {
  prompt: LibraryPrompt
  kind: PromptKind | undefined
  /** The built-in this prompt stands in for, which Changes compares against. */
  original: LibraryPrompt | undefined
  copying: boolean
  onCopy: (body: NewPrompt) => void
  onDeleted: () => void
}) {
  const queryClient = useQueryClient()
  const readOnly = prompt.builtIn
  const [name, setName] = useState(prompt.name)
  const [body, setBody] = useState(prompt.body)
  const [view, setView] = useState<'edit' | 'changes'>('edit')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const save = useAutosave(prompt.id)
  const counts = countText(body)

  const remove = useMutation({
    mutationFn: () => prompts.remove(prompt.id as number),
    onSuccess: () => {
      queryClient.setQueryData<PromptLibrary>(
        LIBRARY_KEY,
        (lib) => lib && { ...lib, prompts: lib.prompts.filter((p) => p.id !== prompt.id) },
      )
      void refreshPreviews(queryClient)
      setConfirmDelete(false)
      toast.success(`Deleted “${name}”`)
      onDeleted()
    },
  })

  const copy = () => {
    save.flush()
    onCopy({
      name: `${name.trim() || 'Untitled'} (copy)`.slice(0, 120),
      kind: prompt.kind,
      body,
      based_on: readOnly ? prompt.kind : name.trim() || null,
    })
  }

  return (
    <Panel
      title={name.trim() || 'Untitled'}
      description={kind?.purpose}
      actions={
        <>
          {!readOnly && <SaveState status={save.status} error={save.error} onRetry={save.flush} />}
          <Button
            size="sm"
            variant={readOnly ? 'primary' : 'secondary'}
            loading={copying}
            onClick={copy}
          >
            <CopyIcon />
            Save a Copy
          </Button>
          {!readOnly && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Delete prompt"
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2Icon />
            </Button>
          )}
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {readOnly ? (
          <Notice tone="info" title="Built-in prompts can't be changed">
            Save a copy to edit it word by word. The copy saves itself as you type, and can be
            chosen wherever {kind?.label ?? 'this prompt'} runs.
          </Notice>
        ) : (
          <Field label="Name">
            <Input
              value={name}
              maxLength={120}
              aria-invalid={!name.trim() || undefined}
              onChange={(e) => {
                setName(e.target.value)
                if (e.target.value.trim()) save.queue({ name: e.target.value })
              }}
              onBlur={save.flush}
            />
          </Field>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3">
          <KV
            items={[
              ['For', kind?.label ?? prompt.kind],
              ...(prompt.basedOn
                ? [
                    [
                      'Copied from',
                      prompt.basedOn === prompt.kind ? `Built-in · ${kind?.label}` : prompt.basedOn,
                    ] as [string, string],
                  ]
                : []),
              [
                'Size',
                `${fmt.int(counts.words)} words · ${fmt.int(counts.characters)} chars · ~${fmt.int(counts.estimatedTokens)} tokens`,
              ],
            ]}
          />
          {!readOnly && original && (
            <Segmented
              label="View"
              items={[
                { value: 'edit', label: 'Edit' },
                { value: 'changes', label: 'Changes' },
              ]}
              value={view}
              onChange={setView}
            />
          )}
        </div>

        {view === 'changes' && original ? (
          <Changes before={original.body} after={body} label={original.name} />
        ) : (
          <Textarea
            aria-label="Prompt text"
            className={TEXT}
            value={body}
            readOnly={readOnly}
            placeholder="Write the instructions the model gets first. Say what to return, and in what shape."
            onChange={(e) => {
              setBody(e.target.value)
              save.queue({ body: e.target.value })
            }}
            onBlur={save.flush}
          />
        )}
        {!readOnly && !body.trim() && (
          <Notice tone="warn" title="This prompt is empty, so a task can't use it yet." />
        )}
      </div>

      <Confirm
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete “${name}”?`}
        confirmLabel="Delete"
        danger
        pending={remove.isPending}
        onConfirm={() => {
          save.discard()
          remove.mutate()
        }}
      >
        A task that already uses it keeps sending the text it had when the task was added.
        {remove.isError && (
          <ErrorNotice className="mt-3" title="Could not delete it" error={remove.error} />
        )}
      </Confirm>
    </Panel>
  )
}

function Changes({ before, after, label }: { before: string; after: string; label: string }) {
  // Deferred: the table is rebuilt from scratch, and typing must not wait on it.
  const text = useDeferredValue(after)
  const parts = diffWords(before, text)
  if (!parts)
    return <Notice tone="info" title="Too different from the built-in to compare word by word." />
  const { added, removed } = diffCounts(parts)
  return (
    <div className="flex flex-col gap-2">
      <p className="num text-body-compact text-ink-subtle">
        Against Built-in · {label}: <span className="text-pnl-positive">+{fmt.int(added)}</span>{' '}
        words, <span className="text-pnl-negative">−{fmt.int(removed)}</span> words
      </p>
      <pre
        className="num max-h-[60vh] overflow-auto rounded-md border border-hairline bg-canvas p-3 text-body-compact leading-relaxed whitespace-pre-wrap text-ink-muted"
        role="region"
        aria-label="Changes from the built-in"
      >
        {parts.map((p, i) =>
          p.op === 'same' ? (
            p.text
          ) : p.op === 'add' ? (
            <ins key={i} className="rounded-xs bg-pnl-positive-tint text-pnl-positive no-underline">
              {p.text}
            </ins>
          ) : (
            <del key={i} className="rounded-xs bg-pnl-negative-tint text-pnl-negative">
              {p.text}
            </del>
          ),
        )}
      </pre>
    </div>
  )
}
