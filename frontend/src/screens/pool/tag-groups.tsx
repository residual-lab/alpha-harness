/**
 * Tags as lists of Alphas, as BRAIN's own lists are: filter by any or all of them, keep a set
 * of tags under a name, bring in what BRAIN holds under a tag, and analyse a group as one
 * portfolio.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DownloadIcon, SaveIcon, XIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { tasks } from '@/api/core'
import type { components } from '@/api/generated'
import { errorMessage } from '@/api/http'
import { fmt } from '@/lib/format'
import { useLive } from '@/lib/live'
import { keepInHarness } from '@/lib/preferences'
import { followOtherTabs } from '@/lib/tabs'
import { useCostBps } from '@/screens/alpha-table'
import { portfolio } from '@/screens/portfolio/api'
import { PortfolioResults } from '@/screens/portfolio/results'
import {
  Button,
  Chips,
  ErrorNotice,
  Input,
  Notice,
  Panel,
  Progress,
  Segmented,
  Skeleton,
} from '@/ui/kit'
import { type AlphaPageRequest, pool } from './api'

type TagGroup = components['schemas']['TagGroup']

/** A tag as BRAIN keeps them. */
const TAG = /^[A-Za-z0-9_-]{1,40}$/
/** Where the analysis keeps its cost, apart from the Portfolio screen's. */
const VIEW = 'tag-group'

/** The tag groups kept under a name, on this computer and in the app's own database. */
const useGroups = create<{ groups: TagGroup[] }>()(
  persist(() => ({ groups: [] as TagGroup[] }), { name: 'alpha-harness-tag-groups', version: 1 }),
)
followOtherTabs(useGroups)
const useKeptGroups = keepInHarness(useGroups, '/api/preferences/tag-groups', 'tag groups')

/** The latest background job of one kind, live while the page holds a connection. */
function useLastJob(kind: string) {
  const live = useLive((s) => s.tasks)
  const polled = useQuery({
    queryKey: ['background-tasks'],
    queryFn: tasks.list,
    enabled: live == null,
    refetchInterval: 3000,
  })
  return ((live ?? polled.data)?.tasks ?? []).filter((t) => t.kind === kind).at(-1)
}

/** Calls ``onDone`` once each time a job of ``kind`` stops running. */
function useWhenDone(kind: string, onDone: () => void) {
  const job = useLastJob(kind)
  const was = useRef(job?.state)
  useEffect(() => {
    if (was.current === 'running' && job?.state !== 'running') onDone()
    was.current = job?.state
  }, [job?.state, onDone])
  return job
}

function JobLine({ job }: { job: ReturnType<typeof useLastJob> }) {
  if (!job) return null
  if (job.state === 'failed')
    return <p className="text-body-compact text-status-error">{job.error ?? 'It stopped.'}</p>
  if (job.state !== 'running')
    return job.detail ? <p className="text-body-compact text-ink-muted">{job.detail}</p> : null
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-body-compact text-ink-muted">{job.detail ?? job.label}</span>
      <Progress value={job.progress ?? null} label={job.label} />
    </div>
  )
}

/** The tag chips, how they combine, and the groups kept under a name. */
export function TagFilter({
  options,
  tags,
  every,
  onChange,
}: {
  options: string[]
  tags: string[]
  every: boolean
  onChange: (tags: string[], every: boolean) => void
}) {
  useKeptGroups()
  const groups = useGroups((s) => s.groups)
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')
  // A saved group may name a tag no stored Alpha carries yet: still offered, so it shows.
  const offered = [...new Set([...options, ...tags])].map((v) => ({ value: v, label: v }))

  const save = () => {
    const label = name.trim()
    if (!label || !tags.length) return
    useGroups.setState((s) => ({
      groups: [
        ...s.groups.filter((g) => g.name !== label),
        { id: crypto.randomUUID(), name: label, tags, every },
      ],
    }))
    setNaming(false)
    setName('')
  }

  return (
    <div className="flex flex-col gap-3">
      {groups.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-body-compact text-ink-subtle">Saved</span>
          {groups.map((g) => (
            <span key={g.id} className="inline-flex items-center">
              <Button
                size="sm"
                variant="ghost"
                title={`${g.every ? 'All of' : 'Any of'}: ${g.tags.join(', ')}`}
                onClick={() => onChange(g.tags, g.every)}
              >
                {g.name}
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={`Forget ${g.name}`}
                onClick={() =>
                  useGroups.setState((s) => ({ groups: s.groups.filter((x) => x.id !== g.id) }))
                }
              >
                <XIcon />
              </Button>
            </span>
          ))}
        </div>
      )}
      {offered.length > 0 && (
        <Chips
          label="Tags"
          items={offered}
          value={tags}
          onChange={(next) => onChange(next, every)}
        />
      )}
      {tags.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {tags.length > 1 && (
            <Segmented
              label="How the tags combine"
              value={every ? 'all' : 'any'}
              onChange={(v) => onChange(tags, v === 'all')}
              items={[
                { value: 'any', label: 'Any of these' },
                { value: 'all', label: 'All of these' },
              ]}
            />
          )}
          {naming ? (
            <form
              className="flex items-center gap-1.5"
              onSubmit={(e) => {
                e.preventDefault()
                save()
              }}
            >
              <Input
                autoFocus
                aria-label="Name for these tags"
                placeholder="e.g. ACE and Power Pool"
                maxLength={60}
                className="w-56"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <Button size="sm" type="submit" disabled={!name.trim()}>
                Save
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setNaming(false)}>
                Cancel
              </Button>
            </form>
          ) : (
            <Button size="sm" variant="ghost" onClick={() => setNaming(true)}>
              <SaveIcon />
              Save as a Group
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

/** Bring in every Alpha BRAIN holds under some tags, made here or elsewhere, e.g. by ACE. */
export function BringInTagged({ suggested }: { suggested: string[] }) {
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const wanted = [...new Set(text.split(/[\s,]+/).filter(Boolean))]
  const invalid = wanted.filter((t) => !TAG.test(t))
  const job = useWhenDone('tagged', () => {
    void queryClient.invalidateQueries({ queryKey: ['pool'] })
  })
  const start = useMutation({
    mutationFn: () => pool.importTagged(wanted),
    onSuccess: () => toast.success(`Bringing in Alphas tagged ${wanted.join(', ')} from BRAIN`),
    onError: (e: unknown) => toast.error(errorMessage(e)),
  })
  const running = job?.state === 'running'
  return (
    <div className="flex flex-col gap-2">
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (wanted.length && !invalid.length) start.mutate()
        }}
      >
        <Input
          aria-label="Tags to bring in from BRAIN"
          placeholder={suggested.length ? suggested.join(', ') : 'e.g. ace_tag, harness'}
          spellCheck={false}
          className="num w-72"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <Button
          size="sm"
          type="submit"
          loading={start.isPending}
          disabled={!wanted.length || invalid.length > 0 || running}
        >
          <DownloadIcon />
          Bring In from BRAIN
        </Button>
      </form>
      <p className="text-body-compact text-ink-subtle">
        {invalid.length
          ? `${invalid.join(', ')}: a tag is letters, digits, _ and - only.`
          : 'Stores every Alpha BRAIN holds under any of these tags, including ones made outside Alpha Harness. Reads only; no simulations.'}
      </p>
      <JobLine job={job} />
    </div>
  )
}

/** The Alphas a filter keeps, analysed as one portfolio at equal weight. */
export function TagAnalysis({
  body,
  onOpen,
}: {
  body: AlphaPageRequest
  onOpen: (alphaId: string, ids?: string[]) => void
}) {
  const queryClient = useQueryClient()
  const cost = useCostBps(VIEW)
  const { limit: _limit, offset: _offset, sort_by: _sort, sort_desc: _desc, ...filters } = body
  const group = useQuery({
    queryKey: ['tag-group', 'ids', filters],
    queryFn: () => pool.group({ ...filters, limit: 1, offset: 0 }),
  })
  const ids = group.data?.alphaIds ?? []
  const computed = useQuery({
    queryKey: ['tag-group', 'compute', ids, cost],
    queryFn: () => portfolio.compute(ids, cost),
    enabled: ids.length > 0,
    placeholderData: keepPreviousData,
    gcTime: 30_000,
  })
  const job = useWhenDone('group-pnl', () => {
    void queryClient.invalidateQueries({ queryKey: ['tag-group'] })
  })
  const download = useMutation({
    mutationFn: (missing: string[]) => pool.downloadPnl(missing),
    onError: (e: unknown) => toast.error(errorMessage(e)),
  })
  const result = ids.length ? computed.data : undefined
  const missing = result?.missing ?? []

  return (
    <div className="flex flex-col gap-4">
      {group.error != null && <ErrorNotice error={group.error} title="Could not read the group" />}
      {computed.error != null && (
        <ErrorNotice error={computed.error} title="Could not combine these Alphas" />
      )}
      {group.data?.more && (
        <Notice tone="info" title={`The newest ${fmt.int(ids.length)} Alphas`}>
          More Alphas match than one analysis combines, so the newest are analysed.
        </Notice>
      )}
      {missing.length > 0 && (
        <Notice
          tone="warn"
          title={`${fmt.int(missing.length)} of these Alphas ${missing.length === 1 ? 'has' : 'have'} no PnL stored`}
          action={
            <Button
              size="sm"
              loading={download.isPending}
              disabled={job?.state === 'running'}
              onClick={() => download.mutate(missing)}
            >
              <DownloadIcon />
              Download PnL
            </Button>
          }
        >
          They are left out until their daily PnL is downloaded from BRAIN. Reads only; no
          simulations.
          <JobLine job={job} />
        </Notice>
      )}
      {group.isPending || (computed.isPending && ids.length > 0) ? (
        <Skeleton className="h-96" label="Combining Alphas" />
      ) : !result || result.alphas === 0 ? (
        <Panel>
          <p className="text-body text-ink-subtle">
            {ids.length === 0
              ? 'No stored Alpha matches these filters.'
              : 'None of these Alphas has PnL stored yet. Download it above to analyse them.'}
          </p>
        </Panel>
      ) : (
        <PortfolioResults
          result={result}
          stale={computed.isPlaceholderData}
          view={VIEW}
          onOpen={onOpen}
        />
      )}
    </div>
  )
}
