/**
 * Ask the assistant. What comes back is a list of real data fields, checked against the
 * catalogue; anything the model invented is shown as dropped. No streaming: one POST per turn.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from '@tanstack/react-router'
import { PlusIcon, Trash2Icon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/api/http'
import { type Scope, scopeLabel, toScopeBody } from '@/api/types'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useScope } from '@/lib/scope'
import {
  budgetDetail,
  type ChatMessage,
  type ChatPick,
  type ChatReply,
  chat,
  type Reasoning,
} from '@/screens/ai/api'
import { DatasetChips, useDatasetTree } from '@/screens/data/dataset-chips'
import { PromptPicker, useChosenPrompt } from '@/screens/prompts/picker'
import {
  Button,
  Empty,
  ErrorNotice,
  Kbd,
  LINK,
  Notice,
  Panel,
  Skeleton,
  Spinner,
  Textarea,
} from '@/ui/kit'
import { Confirm, Select } from '@/ui/overlay'
import { SplitPane } from '@/ui/panels'
import { ScopePicker } from '@/ui/scope-picker'
import { useKeys, useProviderLabel } from './shared'

export function Assistant({ threadId }: { threadId: number | null }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const keys = useKeys()
  const providerLabel = useProviderLabel()
  const options = useQuery({
    queryKey: ['ai', 'chat', 'options'],
    queryFn: chat.options,
    staleTime: 10 * 60 * 1000,
  })
  const threads = useQuery({
    queryKey: ['ai', 'chat', 'threads'],
    queryFn: () => chat.threads(),
  })
  const thread = useQuery({
    queryKey: ['ai', 'chat', 'thread', threadId],
    queryFn: () => chat.thread(threadId as number),
    enabled: threadId !== null,
  })
  const scopes = useQuery({
    queryKey: ['ai', 'downloaded-scopes'],
    queryFn: chat.downloadedScopes,
  })
  const [scope, setScope] = useScope('ai-chat')
  const [text, setText] = useState('')
  const [model, setModel] = useState<string | null>(null)
  const [reasoning, setReasoning] = useState<Reasoning | null>(null)
  const [chosenPrompt, setPrompt] = useState<number | null>(null)
  const promptId = useChosenPrompt('assistant', chosenPrompt)
  const [last, setLast] = useState<ChatReply | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const scroller = useRef<HTMLElement>(null)

  // A saved conversation reopens on the market it was about.
  const threadScope = thread.data?.scope
  useEffect(() => {
    if (threadScope) setScope(threadScope)
  }, [threadScope, setScope])

  const enabledProviders = new Set(keys.data?.keys.filter((k) => k.enabled).map((k) => k.provider))
  // Most requests left today first, so the default is never a model already spent.
  const left = new Map(keys.data?.budget.map((b) => [b.ref, b.remainingToday]))
  const modelItems = (options.data?.models.models ?? [])
    .filter((m) => enabledProviders.has(m.provider))
    .sort((a, b) => (left.get(b.ref) ?? 0) - (left.get(a.ref) ?? 0))
    .map((m) => ({ value: m.ref, label: `${m.id} · ${providerLabel(m.provider)}` }))
  const modelValue =
    (model && modelItems.some((m) => m.value === model) ? model : null) ??
    modelItems[0]?.value ??
    null
  const reasoningValue = reasoning ?? options.data?.defaultReasoning ?? 'normal'
  const reasoningHelp = options.data?.reasoning.find((r) => r.value === reasoningValue)?.description

  const notDownloaded =
    scopes.isSuccess &&
    !scopes.data.some(
      (r) =>
        r.instrument_type === scope.instrumentType &&
        r.region === scope.region &&
        r.delay === scope.delay &&
        r.universe === scope.universe &&
        r.fields > 0,
    )

  const say = useMutation({
    meta: { inline: true },
    mutationFn: (message: string) =>
      chat.say({
        text: message,
        scope: toScopeBody(scope),
        thread_id: threadId,
        model: modelValue,
        reasoning: reasoningValue,
        dataset_ids: [],
        prompt_id: promptId,
      }),
    onSuccess: async (reply) => {
      setLast(reply)
      setText('')
      void queryClient.invalidateQueries({
        queryKey: ['ai', 'chat', 'threads'],
      })
      void queryClient.invalidateQueries({ queryKey: ['ai', 'keys'] })
      void queryClient.invalidateQueries({ queryKey: ['today'] })
      if (reply.threadId !== threadId)
        void navigate({
          to: '/ai/assistant/$threadId',
          params: { threadId: String(reply.threadId) },
        })
      await queryClient.invalidateQueries({
        queryKey: ['ai', 'chat', 'thread', reply.threadId],
      })
    },
  })

  const remove = useMutation({
    mutationFn: (id: number) => chat.deleteThread(id),
    onSuccess: () => {
      toast.success('Conversation deleted')
      setConfirmDelete(false)
      void queryClient.invalidateQueries({
        queryKey: ['ai', 'chat', 'threads'],
      })
      void navigate({ to: '/ai/$tab', params: { tab: 'assistant' } })
    },
  })

  const messages = threadId !== null ? (thread.data?.messages ?? []) : []
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run to pin the view to the newest message whenever one arrives
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight })
  }, [messages.length, say.isPending])

  if (keys.isError) return <ErrorNotice title="Could not load the Keys" error={keys.error} />
  if (!keys.data) return <Skeleton className="h-96" />
  if (keys.data.enabled === 0) {
    return (
      <Panel>
        <Empty title="The assistant needs a Key">
          Add a free Key from any provider. It takes a minute, needs no card, and lets you describe
          an idea in your own words and get back real data fields.{' '}
          <Link to="/ai/$tab" params={{ tab: 'providers' }} className={LINK}>
            Choose a provider
          </Link>
        </Empty>
      </Panel>
    )
  }
  if (options.isSuccess && modelItems.length === 0) {
    return (
      <Panel>
        <Empty title="The assistant needs a model">
          Your Key is in. Now pick a model it can reach and give it the limits your provider shows
          you.{' '}
          <Link to="/ai/$tab" params={{ tab: 'models' }} className={LINK}>
            Set up a model
          </Link>
        </Empty>
      </Panel>
    )
  }

  const extrasFor = last && last.threadId === threadId ? last : null
  const lastAssistantId = messages.findLast((m) => m.role === 'assistant')?.id
  // `scopes.isPending` blocks sending: until the answer is in, an undownloaded market looks
  // downloaded and the send fails.
  const canSend =
    !!text.trim() && !say.isPending && !notDownloaded && !scopes.isPending && modelValue !== null
  const send = () => canSend && say.mutate(text.trim())
  const detail = budgetDetail(say.error)

  return (
    <>
      <SplitPane id="ai-assistant" first={{ default: 260, min: 200, max: 420 }}>
        <Panel
          title="Conversations"
          bodyClassName="p-2"
          actions={
            <Button size="sm" render={<Link to="/ai/$tab" params={{ tab: 'assistant' }} />}>
              <PlusIcon />
              New
            </Button>
          }
        >
          {threads.isError ? (
            <ErrorNotice title="Could not load conversations" error={threads.error} />
          ) : !threads.data ? (
            <Skeleton className="h-48" />
          ) : threads.data.length === 0 ? (
            <Empty title="No conversations yet" />
          ) : (
            <ul className="-m-1 flex max-h-[70vh] flex-col gap-0.5 overflow-auto p-1">
              {threads.data.map((t) => (
                <li key={t.id}>
                  <Link
                    to="/ai/assistant/$threadId"
                    params={{ threadId: String(t.id) }}
                    className={cn(
                      'flex flex-col gap-0.5 rounded-md px-2 py-1.5 transition-colors hover:bg-surface-2',
                      t.id === threadId && 'bg-surface-2',
                    )}
                    aria-current={t.id === threadId ? 'page' : undefined}
                  >
                    <span
                      className={cn(
                        'truncate text-body',
                        t.id === threadId ? 'text-ink' : 'text-ink-muted',
                      )}
                    >
                      {t.title}
                    </span>
                    <span className="num truncate text-body-compact text-ink-subtle">
                      {t.scope} · {fmt.ago(t.updatedAt)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          title={thread.data?.title ?? 'New conversation'}
          description="Describe a hunch in your own words. The assistant answers with fields that really exist in this market."
          actions={
            <>
              {/* Fixed once a conversation exists: the backend pins a thread to the market it
                  started in. */}
              <ScopePicker
                scope={scope}
                onChange={setScope}
                disabled={say.isPending || threadId !== null}
              />
              {thread.data && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Delete conversation"
                  onClick={() => setConfirmDelete(true)}
                >
                  <Trash2Icon />
                </Button>
              )}
            </>
          }
          bodyClassName="flex flex-col gap-3"
        >
          {thread.isError && (
            <ErrorNotice title="Could not open this conversation" error={thread.error} />
          )}

          {/* A labelled section, not a div with role="region": same landmark, native element. */}
          <section
            ref={scroller}
            aria-label="Messages"
            className="flex h-112 max-h-[70vh] flex-col gap-4 overflow-auto rounded-md border border-hairline bg-canvas p-4"
          >
            {messages.length === 0 &&
              !say.isPending &&
              (thread.isLoading ? (
                <Skeleton className="h-24" />
              ) : (
                <Empty title="Start with an idea" className="m-auto">
                  For example: “companies whose analysts keep raising their earnings estimates”.
                  Each message uses one request from your assistant budget.
                </Empty>
              ))}
            {messages.map((m) => (
              <Turn
                key={m.id}
                message={m}
                reply={m.id === lastAssistantId ? extrasFor : null}
                scope={scope}
              />
            ))}
            {say.isPending && (
              <>
                <UserBubble text={say.variables} />
                <p className="flex items-center gap-2 text-body-compact text-ink-subtle">
                  <Spinner className="size-3.5" />
                  Reading the catalogue and thinking…
                </p>
              </>
            )}
          </section>

          {say.isError && (
            <Notice tone="error" title="The assistant could not answer">
              {errorMessage(say.error)}
              {detail && <span className="num mt-1 block text-body-compact">{detail}</span>}
            </Notice>
          )}

          {notDownloaded && (
            <Notice tone="warn" title="Download this market first">
              <span className="num">{scopeLabel(scope)}</span> is not in the local catalogue, so the
              assistant has no fields to choose from.{' '}
              <Link to="/data" className={LINK}>
                Download it in Data
              </Link>
            </Notice>
          )}
          {options.isError && (
            <ErrorNotice title="Could not load the model list" error={options.error} />
          )}
          {scopes.isError && (
            <ErrorNotice
              title="Could not check which markets are downloaded"
              error={scopes.error}
            />
          )}

          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              send()
            }}
          >
            <Textarea
              aria-label="Message"
              placeholder="Describe an idea in your own words"
              value={text}
              disabled={say.isPending}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault()
                  send()
                }
              }}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Select
                label="Model"
                mono
                className="max-w-72"
                items={modelItems}
                value={modelValue}
                onChange={setModel}
                disabled={say.isPending || modelItems.length === 0}
              />
              <Select
                label="Reasoning"
                items={(options.data?.reasoning ?? []).map((r) => ({
                  value: r.value,
                  label: r.label,
                }))}
                value={reasoningValue}
                onChange={setReasoning}
                disabled={say.isPending}
              />
              <PromptPicker
                kind="assistant"
                className="max-w-56"
                value={promptId}
                onChange={setPrompt}
                disabled={say.isPending}
              />
              <span className="ml-auto hidden items-center gap-1 sm:inline-flex">
                <Kbd>Ctrl</Kbd>
                <Kbd>Enter</Kbd>
              </span>
              <Button variant="primary" type="submit" loading={say.isPending} disabled={!canSend}>
                Send
              </Button>
            </div>
            <p className="text-body-compact text-pretty text-ink-subtle">
              {reasoningHelp ? `${reasoningHelp} ` : ''}Each message spends one assistant request.
              {options.data?.note ? ` ${options.data.note}` : ''}
            </p>
          </form>
        </Panel>
      </SplitPane>

      <Confirm
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete this conversation?"
        confirmLabel="Delete"
        danger
        pending={remove.isPending}
        onConfirm={() => thread.data && remove.mutate(thread.data.id)}
      >
        “{thread.data?.title}” and its{' '}
        <span className="num">{fmt.int(thread.data?.messages.length)}</span> messages are removed
        from this machine.
      </Confirm>
    </>
  )
}

/** The datasets a reply drew its fields from, placed in the chat's market. */
function ReplyDatasets({ scope, ids }: { scope: Scope; ids: string[] }) {
  const { tree, nameOf, ready } = useDatasetTree(scope)
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-body-compact text-ink-subtle">Datasets</span>
      <DatasetChips tree={tree} value={ids} nameOf={nameOf} ready={ready} />
    </div>
  )
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="max-w-[85%] self-end rounded-lg bg-surface-2 px-3 py-2 text-body break-words whitespace-pre-wrap text-ink">
      {text}
    </div>
  )
}

function Turn({
  message,
  reply,
  scope,
}: {
  message: ChatMessage
  reply: ChatReply | null
  scope: Scope
}) {
  if (message.role === 'user') return <UserBubble text={message.text} />

  const { meta } = message
  // The live reply carries the fuller token breakdown; everything else is stored on the
  // message, so it survives a reload rather than disappearing with the component's state.
  const datasets = reply?.datasets ?? meta.datasets ?? []
  const dropped = reply?.dropped ?? meta.dropped ?? []
  const catalogNote = reply?.catalogNote ?? meta.catalogNote
  const usage = reply
    ? `${reply.model} · ${reply.reasoning} · ${fmt.int(reply.usage.promptTokens)} in · ${fmt.int(reply.usage.outputTokens)} out · ${fmt.int(reply.usage.thinkingTokens)} thinking tokens`
    : [
        meta.model,
        meta.reasoning,
        meta.tokens !== undefined ? `${fmt.int(meta.tokens)} tokens` : null,
      ]
        .filter(Boolean)
        .join(' · ')

  return (
    <div className="flex max-w-[92%] flex-col gap-2">
      <p className="text-body text-pretty break-words whitespace-pre-wrap text-ink-muted">
        {message.text}
      </p>
      <Picks picks={reply?.picks ?? meta.picks ?? []} />
      {datasets.length > 0 && <ReplyDatasets scope={scope} ids={datasets} />}
      {dropped.length > 0 && (
        <Notice tone="warn" title="Named by the model but not in the catalogue, so dropped">
          <span className="num">{dropped.join(', ')}</span>
        </Notice>
      )}
      {catalogNote && (
        <p className="text-body-compact text-pretty text-ink-subtle">{catalogNote}</p>
      )}
      {usage && <p className="num text-body-compact text-ink-subtle">{usage}</p>}
    </div>
  )
}

function Picks({ picks }: { picks: ChatPick[] }) {
  if (picks.length === 0) return null
  return (
    <div className="flex flex-col gap-1 rounded-md border border-hairline bg-surface-1 p-2">
      <p className="text-body-compact text-ink-subtle">
        Fields picked (<span className="num">{fmt.int(picks.length)}</span>)
      </p>
      {picks.map((p) => (
        <div key={p.field} className="flex flex-wrap items-baseline gap-2 text-body-compact">
          <Link to="/data" className="num text-ink transition-colors hover:text-link">
            {p.field}
          </Link>
          <span className="text-pretty text-ink-subtle">{p.why}</span>
        </div>
      ))}
    </div>
  )
}
