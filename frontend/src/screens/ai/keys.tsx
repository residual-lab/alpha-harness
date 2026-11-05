import { useMutation } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/api/http'
import { DASH, fmt } from '@/lib/format'
import { type LLMKey, llm } from '@/screens/ai/api'
import { Badge, Button, Checkbox, Empty, ErrorNotice, Input, LINK, Panel, Skeleton } from '@/ui/kit'
import { Confirm } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'
import { useInvalidateKeys, useKeys, useProviderLabel } from './shared'

const nameOf = (k: LLMKey) => k.label || k.hint

export function Keys() {
  const keys = useKeys()
  const providerLabel = useProviderLabel()
  const invalidate = useInvalidateKeys()
  const [deleting, setDeleting] = useState<LLMKey | null>(null)

  const checkAll = useMutation({
    mutationFn: llm.checkAll,
    onSuccess: (results) => {
      const failures = results.flatMap((r) => (r.ok ? [] : [`Key ${r.keyId}: ${r.error}`]))
      const summary = `${fmt.int(results.length - failures.length)} of ${fmt.int(results.length)} Keys work`
      if (failures.length) toast.error(summary, { description: failures.join('\n') })
      else toast.success(summary)
      invalidate()
    },
  })

  const remove = useMutation({
    mutationFn: (k: LLMKey) => llm.removeKey(k.id),
    onSuccess: (_, k) => {
      toast.success(`Removed ${nameOf(k)}`)
      setDeleting(null)
      invalidate()
    },
  })

  if (keys.isError) return <ErrorNotice title="Could not load the Keys" error={keys.error} />
  if (!keys.data) return <Skeleton className="h-64" />

  const { data } = keys
  if (data.keys.length === 0) {
    return (
      <Panel>
        <Empty title="No assistant Keys yet">
          A Key is free, takes a minute, and is what lets the assistant explain the data to you.{' '}
          <Link to="/ai/$tab" params={{ tab: 'providers' }} className={LINK}>
            Choose a provider
          </Link>
        </Empty>
      </Panel>
    )
  }

  const columns: Column<LLMKey>[] = [
    {
      key: 'provider',
      header: 'Provider',
      width: 'minmax(120px,1fr)',
      cell: (k) => providerLabel(k.provider),
    },
    {
      key: 'label',
      header: 'Label',
      width: 'minmax(120px,1fr)',
      cell: (k) => k.label || <span className="text-ink-subtle">{DASH}</span>,
    },
    {
      key: 'hint',
      header: 'Key',
      width: '130px',
      cell: (k) => <span className="num text-ink-muted">{k.hint}</span>,
    },
    {
      key: 'enabled',
      header: 'Enabled',
      width: '80px',
      cell: (k) => <EnabledCell apiKey={k} />,
    },
    {
      key: 'status',
      header: 'Last check',
      // Fits the longest badge, "Working · 59 min ago": it cannot wrap.
      width: '200px',
      cell: (k) => <KeyStatus apiKey={k} />,
    },
    {
      key: 'cap',
      header: 'Daily cap',
      width: '110px',
      align: 'right',
      cell: (k) => <CapCell apiKey={k} />,
    },
    {
      key: 'requests',
      header: 'Requests today',
      width: '120px',
      align: 'right',
      cell: (k) => fmt.int(k.usage.reduce((s, u) => s + u.requests, 0)),
    },
    {
      key: 'tokens',
      header: 'Tokens today',
      width: '120px',
      align: 'right',
      cell: (k) => fmt.int(k.usage.reduce((s, u) => s + u.tokens, 0)),
    },
    {
      key: 'actions',
      header: '',
      width: '150px',
      cell: (k) => <RowActions apiKey={k} onDelete={() => setDeleting(k)} />,
    },
  ]

  return (
    <Panel
      title="Key Pool"
      description={`${fmt.int(data.enabled)} of ${fmt.int(data.keys.length)} Keys enabled. Requests rotate across enabled Keys; usage counts each model's current day.`}
      bodyClassName="p-0"
      actions={
        <Button size="sm" loading={checkAll.isPending} onClick={() => checkAll.mutate()}>
          Check all
        </Button>
      }
    >
      <DataTable
        label="API keys"
        rows={data.keys}
        columns={columns}
        rowKey={(k) => String(k.id)}
        rowHeight={40}
      />
      <Confirm
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Remove ${deleting ? nameOf(deleting) : 'Key'}?`}
        confirmLabel="Remove Key"
        danger
        pending={remove.isPending}
        onConfirm={() => deleting && remove.mutate(deleting)}
      >
        The Key is deleted from the local vault and its daily budget leaves the pool. You can add it
        again later.
      </Confirm>
    </Panel>
  )
}

function EnabledCell({ apiKey: k }: { apiKey: LLMKey }) {
  const invalidate = useInvalidateKeys()
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => llm.setEnabled(k.id, enabled),
    onSuccess: (updated) => {
      toast.success(`${nameOf(k)} ${updated.enabled ? 'enabled' : 'disabled'}`)
      invalidate()
    },
  })
  return (
    <Checkbox
      label={null}
      aria-label={`Enable ${nameOf(k)}`}
      checked={k.enabled}
      disabled={toggle.isPending}
      onChange={(next) => toggle.mutate(next)}
    />
  )
}

/** The key's own daily ceiling, editable in place.
 *
 * Editable because it is a spending limit on a paid account: the moment someone wants to
 * lower one is the moment they have found it too high, and "delete the key and add it
 * again" is not what anyone wants to hear then. A key with no cap of its own stops at the
 * limits of the models set up for it.
 */
function CapCell({ apiKey: k }: { apiKey: LLMKey }) {
  const invalidate = useInvalidateKeys()
  const [draft, setDraft] = useState(String(k.dailyLimit ?? ''))
  // Follows the row when the table refetches, which `useState` alone does not: the cap can
  // change under this cell from another tab, or from the mutation below.
  const [seen, setSeen] = useState(k.dailyLimit)
  if (seen !== k.dailyLimit) {
    setSeen(k.dailyLimit)
    setDraft(String(k.dailyLimit ?? ''))
  }
  const save = useMutation({
    // null clears it: a cap you cannot take off is a trap on a free key, which never
    // needed one. A paid key's cap is refused by the backend, which says so.
    mutationFn: (cap: number | null) => llm.setEnabled(k.id, k.enabled, cap),
    onSuccess: (updated) => {
      toast.success(
        updated.dailyLimit === null
          ? `${nameOf(k)} has no daily cap`
          : `${nameOf(k)} capped at ${fmt.int(updated.dailyLimit)} requests a day`,
      )
      invalidate()
    },
    onError: (e) => {
      toast.error(errorMessage(e))
      setDraft(String(k.dailyLimit ?? ''))
    },
  })

  // Escape leaves the field too, and leaving it saves: this keeps that one from saving.
  const dropped = useRef(false)
  const commit = () => {
    if (dropped.current) {
      dropped.current = false
      return
    }
    const clean = draft.trim()
    if (clean === '') {
      if (k.dailyLimit !== null) save.mutate(null)
      return
    }
    const next = Number.parseInt(clean, 10)
    if (!Number.isFinite(next) || next < 1 || next === k.dailyLimit) {
      setDraft(String(k.dailyLimit ?? ''))
      return
    }
    save.mutate(next)
  }

  return (
    <Input
      type="number"
      min={1}
      className="h-7 w-full text-right"
      aria-label={`Daily cap for ${nameOf(k)}`}
      placeholder="None"
      value={draft}
      disabled={save.isPending}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.preventDefault()
          dropped.current = true
          setDraft(String(k.dailyLimit ?? ''))
          e.currentTarget.blur()
        } else if (e.key === 'Enter') e.currentTarget.blur()
      }}
    />
  )
}

function RowActions({ apiKey: k, onDelete }: { apiKey: LLMKey; onDelete: () => void }) {
  const invalidate = useInvalidateKeys()
  const check = useMutation({
    mutationFn: () => llm.checkKey(k.id),
    onSuccess: (result) => {
      if (result.ok) toast.success(`${nameOf(k)} works · reaches ${fmt.int(result.models)} models`)
      else
        toast.error(`${nameOf(k)} failed the check`, {
          description: result.error,
        })
      invalidate()
    },
  })
  return (
    <div className="flex w-full justify-end gap-1">
      <Button variant="ghost" size="sm" loading={check.isPending} onClick={() => check.mutate()}>
        Check
      </Button>
      <Button variant="danger" size="sm" onClick={onDelete}>
        Delete
      </Button>
    </div>
  )
}

function KeyStatus({ apiKey: k }: { apiKey: LLMKey }) {
  if (k.lastError)
    return (
      <Badge tone="loss" title={k.lastError}>
        Failing
      </Badge>
    )
  if (k.lastOkAt) return <Badge tone="profit">Working · {fmt.ago(k.lastOkAt)}</Badge>
  return <Badge tone="muted">Unchecked</Badge>
}
