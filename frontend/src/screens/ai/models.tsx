/**
 * Models the assistant may use. Nothing is built in: the user picks a model their Key
 * lists, or types one, and copies its limits from their own provider account.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { ExternalLinkIcon, PlusIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { type LLMModel, type LLMProvider, llm, type SetModelRequest } from '@/screens/ai/api'
import {
  Button,
  Empty,
  ErrorNotice,
  Field,
  Input,
  Notice,
  Panel,
  Progress,
  Skeleton,
} from '@/ui/kit'
import { Confirm, Dialog, Select } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'
import { useInvalidateKeys, useKeys, useModels, useProviderLabel, useProviders } from './shared'

/** Where a provider's first model starts; later ones start from the zone chosen for it before. */
const DEFAULT_TIME_ZONE = 'America/Los_Angeles'

/** A zone's offset right now, e.g. "UTC-7", so a name can be recognised by its hours. */
const offset = (timeZone: string) => {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone, timeZoneName: 'shortOffset' })
  const name = parts.formatToParts(new Date()).find((p) => p.type === 'timeZoneName')?.value
  return (name ?? 'GMT').replace('GMT', 'UTC')
}

/** Every time zone this browser knows. Some browsers leave UTC itself out of the list. */
const TIME_ZONES = ['UTC', ...Intl.supportedValuesOf('timeZone').filter((z) => z !== 'UTC')].map(
  (zone) => ({ value: zone, label: `${zone} · ${offset(zone)}` }),
)

/** Rows the model list draws. Some providers list thousands; typing narrows the rest. */
const SHOWN = 100

/** A limit as typed: a whole number of at least one, or nothing yet. */
const requests = (text: string) => {
  const n = Number(text)
  return text.trim() !== '' && Number.isInteger(n) && n >= 1 ? n : null
}

/** Set one model up, or change the limits of one that is. */
export function ModelSetupForm({
  provider,
  model,
  onSaved,
}: {
  provider: LLMProvider
  /** Editing this one: its id is fixed, only the limits change. */
  model?: LLMModel | undefined
  onSaved: (saved: LLMModel) => void
}) {
  const invalidate = useInvalidateKeys()
  const models = useModels()
  const offered = useQuery({
    queryKey: ['ai', 'offered', provider.id],
    queryFn: () => llm.offered(provider.id),
    enabled: !model,
    staleTime: 5 * 60 * 1000,
  })
  const [id, setId] = useState(model?.id ?? '')
  const [rpm, setRpm] = useState(model ? String(model.rpm) : '')
  const [rpd, setRpd] = useState(model ? String(model.rpd) : '')
  const [maxTokens, setMaxTokens] = useState(model?.maxPromptTokens?.toString() ?? '')
  const [timeZone, setTimeZone] = useState(
    model?.resetTimezone ??
      models.data?.models.find((m) => m.provider === provider.id)?.resetTimezone ??
      DEFAULT_TIME_ZONE,
  )

  const save = useMutation({
    mutationFn: (body: SetModelRequest) => llm.setModel(body),
    onSuccess: (saved) => {
      toast.success(model ? `Limits saved for ${saved.id}` : `${saved.id} is set up`)
      invalidate()
      onSaved(saved)
    },
  })

  const perMinute = requests(rpm)
  const perDay = requests(rpd)
  // Optional: empty is the default, anything else has to be a real ceiling.
  const promptTokens = maxTokens.trim() === '' ? null : requests(maxTokens)
  const tokensInvalid = maxTokens.trim() !== '' && promptTokens === null
  const chosen = id.trim()
  const listed = offered.data?.models ?? []
  const matches = listed.filter((m) => m.toLowerCase().includes(chosen.toLowerCase()))
  const submit = () => {
    if (!chosen || perMinute === null || perDay === null || tokensInvalid) return
    save.mutate({
      provider: provider.id,
      model: chosen,
      requests_per_minute: perMinute,
      requests_per_day: perDay,
      reset_timezone: timeZone,
      max_prompt_tokens: promptTokens,
    })
  }

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      <div className="flex flex-col gap-2">
        <Field
          label="Model ID"
          hint={model ? 'To use a different model, set that one up and remove this one.' : null}
        >
          <Input
            className="num"
            value={id}
            disabled={model !== undefined}
            autoComplete="off"
            spellCheck={false}
            placeholder={
              listed.length > 0
                ? `Search ${fmt.int(listed.length)} models, or type an ID`
                : 'Type the model ID'
            }
            onChange={(e) => setId(e.target.value)}
          />
        </Field>
        {!model &&
          (offered.isPending ? (
            <Skeleton className="h-40" label={`Asking ${provider.label} for its models`} />
          ) : offered.data?.error ? (
            <Notice tone="warn" title={`${provider.label} did not list its models`}>
              {offered.data.error} Type the ID from {provider.label}'s documentation instead.
            </Notice>
          ) : (
            listed.length > 0 && (
              <ul
                aria-label={`Models ${provider.label} lists for this Key`}
                className="flex max-h-48 flex-col overflow-auto rounded-md border border-hairline bg-canvas p-1"
              >
                {matches.slice(0, SHOWN).map((m) => (
                  <li key={m}>
                    <button
                      type="button"
                      aria-pressed={m === chosen}
                      onClick={() => setId(m)}
                      className={cn(
                        'num w-full truncate rounded-sm px-2 py-1 text-left text-body-compact transition-colors hover:bg-surface-2 hover:text-ink',
                        m === chosen ? 'bg-surface-2 text-ink' : 'text-ink-muted',
                      )}
                    >
                      {m}
                    </button>
                  </li>
                ))}
                {matches.length > SHOWN && (
                  <li className="px-2 py-1 text-body-compact text-ink-subtle">
                    Showing <span className="num">{fmt.int(SHOWN)}</span> of{' '}
                    <span className="num">{fmt.int(matches.length)}</span>. Type to narrow the list.
                  </li>
                )}
                {matches.length === 0 && (
                  <li className="px-2 py-1 text-body-compact text-pretty text-ink-subtle">
                    {provider.label} does not list this ID. It will be sent exactly as typed.
                  </li>
                )}
              </ul>
            )
          ))}
      </div>

      <section
        aria-label="Limits"
        className="flex flex-col gap-3 rounded-md border border-hairline bg-surface-1 p-3"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <p className="min-w-60 flex-1 text-body-compact text-pretty text-ink-muted">
            Copy this model's limits from your {provider.label} account. Alpha Harness stops each
            Key at them, so it never walks into the provider's own wall.
          </p>
          <Button
            variant="secondary"
            size="sm"
            className="shrink-0"
            render={<a href={provider.limitsUrl} target="_blank" rel="noopener noreferrer" />}
          >
            {provider.label} Rate Limits
            <ExternalLinkIcon />
          </Button>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label="Requests per Minute"
            error={rpm !== '' && perMinute === null ? 'A whole number, at least 1.' : null}
          >
            <Input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              className="num"
              value={rpm}
              aria-invalid={rpm !== '' && perMinute === null}
              onChange={(e) => setRpm(e.target.value)}
            />
          </Field>
          <Field
            label="Requests per Day"
            error={rpd !== '' && perDay === null ? 'A whole number, at least 1.' : null}
          >
            <Input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              className="num"
              value={rpd}
              aria-invalid={rpd !== '' && perDay === null}
              onChange={(e) => setRpd(e.target.value)}
            />
          </Field>
          <Field
            className="sm:col-span-2"
            label="Day Resets at Midnight In"
            hint={`When ${provider.label} starts counting a new day for this limit. Its Rate Limits page usually says; if it does not, keep Pacific.`}
          >
            <Select
              label="Day resets at midnight in"
              mono
              items={TIME_ZONES}
              value={timeZone}
              onChange={setTimeZone}
            />
          </Field>
          <Field
            label="Max Prompt Tokens"
            hint={`Optional. The most one Power Pool prompt may use: lower it for a model with a small context or tokens-per-minute limit. Empty uses ${fmt.int(models.data?.defaultPromptTokens)}.`}
            error={tokensInvalid ? 'A whole number, at least 1, or empty.' : null}
          >
            <Input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              className="num"
              placeholder={fmt.int(models.data?.defaultPromptTokens)}
              value={maxTokens}
              aria-invalid={tokensInvalid}
              onChange={(e) => setMaxTokens(e.target.value)}
            />
          </Field>
        </div>
        {provider.paid && (
          <p className="text-body-compact text-pretty text-ink-subtle">
            Each Key's own daily cap still applies. Whichever is lower stops it.
          </p>
        )}
      </section>

      <div className="flex justify-end">
        <Button
          variant="primary"
          type="submit"
          loading={save.isPending}
          disabled={!chosen || perMinute === null || perDay === null || tokensInvalid}
        >
          {model ? 'Save limits' : 'Set up Model'}
        </Button>
      </div>
    </form>
  )
}

interface Editing {
  model?: LLMModel
  provider?: string
}

/** The form in a dialog, with a provider to choose when more than one has a Key. */
function ModelDialog({ editing, onClose }: { editing: Editing | null; onClose: () => void }) {
  const providers = useProviders()
  const keys = useKeys()
  const [picked, setPicked] = useState<string | null>(null)

  const keyed = new Set(
    keys.data?.keys.filter((k) => k.enabled && !k.retired).map((k) => k.provider),
  )
  const choices = (providers.data?.providers ?? []).filter((p) => keyed.has(p.id))
  const wanted = editing?.model?.provider ?? editing?.provider ?? picked
  const provider = choices.find((p) => p.id === wanted) ?? choices[0]
  const close = () => {
    setPicked(null)
    onClose()
  }

  return (
    <Dialog
      open={editing !== null}
      onOpenChange={(open) => !open && close()}
      className="max-w-2xl"
      title={
        editing?.model
          ? `Limits for ${editing.model.id}`
          : provider && (editing?.provider || choices.length === 1)
            ? `Set up a model for ${provider.label}`
            : 'Set up a model'
      }
    >
      {!provider ? (
        <Empty title="No enabled Key">Add a Key under Providers first.</Empty>
      ) : (
        <div className="flex flex-col gap-4">
          {!editing?.model && !editing?.provider && choices.length > 1 && (
            <Field label="Provider">
              <Select
                label="Provider"
                items={choices.map((p) => ({ value: p.id, label: p.label }))}
                value={provider.id}
                onChange={setPicked}
              />
            </Field>
          )}
          <ModelSetupForm
            key={`${provider.id}/${editing?.model?.id ?? ''}`}
            provider={provider}
            model={editing?.model}
            onSaved={close}
          />
        </div>
      )}
    </Dialog>
  )
}

export function Models() {
  const models = useModels()
  const keys = useKeys()
  const providerLabel = useProviderLabel()
  const invalidate = useInvalidateKeys()
  const [editing, setEditing] = useState<Editing | null>(null)
  const [removing, setRemoving] = useState<LLMModel | null>(null)

  const remove = useMutation({
    mutationFn: (m: LLMModel) => llm.removeModel(m.provider, m.id),
    onSuccess: (_, m) => {
      toast.success(`Removed ${m.id}`)
      setRemoving(null)
      invalidate()
    },
  })

  if (models.isError) return <ErrorNotice title="Could not load the models" error={models.error} />
  if (!models.data || !keys.data) return <Skeleton className="h-64" />

  const setUp = models.data.models
  const keyed = [
    ...new Set(keys.data.keys.filter((k) => k.enabled && !k.retired).map((k) => k.provider)),
  ]
  const bare = keyed.filter((p) => !setUp.some((m) => m.provider === p))
  const budget = new Map(keys.data.budget.map((b) => [b.ref, b]))

  const columns: Column<LLMModel>[] = [
    {
      key: 'model',
      header: 'Model ID',
      width: 'minmax(200px,1.6fr)',
      cell: (m) => (
        <span className="num truncate text-ink" title={m.id}>
          {m.id}
        </span>
      ),
    },
    {
      key: 'provider',
      header: 'Provider',
      width: 'minmax(120px,0.8fr)',
      cell: (m) => providerLabel(m.provider),
    },
    {
      key: 'rpm',
      header: 'Per Minute',
      width: '100px',
      align: 'right',
      cell: (m) => fmt.int(m.rpm),
    },
    {
      key: 'rpd',
      header: 'Per Day',
      width: '100px',
      align: 'right',
      cell: (m) => fmt.int(m.rpd),
    },
    {
      key: 'prompt',
      header: 'Max Prompt',
      width: '110px',
      align: 'right',
      cell: (m) =>
        m.maxPromptTokens === null ? (
          <span className="text-ink-subtle">Default</span>
        ) : (
          fmt.int(m.maxPromptTokens)
        ),
    },
    {
      key: 'resets',
      header: 'Resets',
      width: 'minmax(150px,0.8fr)',
      cell: (m) => (
        <span className="num truncate text-ink-muted" title={`Midnight, ${m.resetTimezone}`}>
          {m.resetTimezone}
        </span>
      ),
    },
    {
      key: 'left',
      header: 'Left today',
      width: 'minmax(180px,1fr)',
      cell: (m) => {
        const b = budget.get(m.ref)
        if (m.retired) return <span className="text-ink-subtle">Retired</span>
        if (!b || b.allowedToday === 0)
          return <span className="text-ink-subtle">No enabled Key</span>
        return (
          <div className="flex w-full items-center gap-2">
            <Progress
              className="flex-1"
              value={b.remainingToday / b.allowedToday}
              label={`${m.id} requests left today`}
            />
            <span className="num shrink-0 text-body-compact text-ink-muted">
              {fmt.int(b.remainingToday)} / {fmt.int(b.allowedToday)}
            </span>
          </div>
        )
      },
    },
    {
      key: 'actions',
      header: '',
      width: '150px',
      cell: (m) => (
        <div className="flex w-full justify-end gap-1">
          {!m.retired && (
            <Button variant="ghost" size="sm" onClick={() => setEditing({ model: m })}>
              Edit limits
            </Button>
          )}
          <Button variant="danger" size="sm" onClick={() => setRemoving(m)}>
            Remove
          </Button>
        </div>
      ),
    },
  ]

  return (
    <div className="flex flex-col gap-3">
      {setUp.some((m) => m.retired) && (
        <Notice tone="warn" title="Some models belong to a provider that no longer exists">
          {[...new Set(setUp.flatMap((m) => (m.retired ? [m.retired] : [])))].join(' ')} These
          models are never used. Remove them below.
        </Notice>
      )}
      {bare.map((p) => (
        <Notice
          key={p}
          tone="warn"
          title={`${providerLabel(p)} has a Key but no model`}
          action={
            <Button size="sm" onClick={() => setEditing({ provider: p })}>
              Set up a model
            </Button>
          }
        >
          The assistant cannot use this Key until a model is set up for it.
        </Notice>
      ))}
      <Panel
        title="Models"
        description={`${models.data.note} Limits are yours to set: copy them from each provider's rate-limit page.`}
        bodyClassName="p-0"
        actions={
          <Button
            size="sm"
            variant="primary"
            disabled={keyed.length === 0}
            onClick={() => setEditing({})}
          >
            <PlusIcon />
            Set up a model
          </Button>
        }
      >
        {setUp.length === 0 ? (
          <Empty title="No model set up yet" className="m-4">
            Pick a model your Key can reach and give it the limits your provider shows you. The
            assistant and the Power Pool Lab use only the models set up here.
          </Empty>
        ) : (
          <DataTable
            label="Models"
            rows={setUp}
            columns={columns}
            rowKey={(m) => m.ref}
            rowHeight={40}
          />
        )}
      </Panel>
      <ModelDialog editing={editing} onClose={() => setEditing(null)} />
      <Confirm
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title={`Remove ${removing?.id ?? 'model'}?`}
        confirmLabel="Remove model"
        danger
        pending={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing)}
      >
        The assistant stops using it, and Power Pool tasks on it pause until it is set up again.
        Today's request counts are kept.
      </Confirm>
    </div>
  )
}
