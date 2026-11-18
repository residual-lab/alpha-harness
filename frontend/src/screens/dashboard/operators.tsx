/** Every operator a regular Alpha can use, and how many of the quarter's submitted Alphas use it. */

import type { ReactNode } from 'react'
import type { components } from '@/api/generated'
import { http } from '@/api/http'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { Notice } from '@/ui/kit'
import { Dialog } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'

export type OperatorUsage = components['schemas']['OperatorUsage']
type OperatorUse = components['schemas']['OperatorUse']

/** One read of the quarter's submissions from BRAIN, shared by every screen that shows it. It
 * moves only when something is submitted, so it is not worth asking for often. */
export const operatorUsage = {
  queryKey: ['quarter', 'operators'],
  queryFn: () => http.get<OperatorUsage>('/api/quarter/operators'),
  staleTime: 10 * 60 * 1000,
}

const COLUMNS: Column<OperatorUse>[] = [
  {
    key: 'name',
    header: 'Operator',
    width: 'minmax(160px,2fr)',
    cell: (r) => <span className="num truncate text-ink">{r.name}</span>,
  },
  {
    key: 'category',
    header: 'Category',
    width: 'minmax(120px,1fr)',
    cell: (r) => <span className="truncate">{r.category || DASH}</span>,
  },
  {
    key: 'alphas',
    header: 'Alphas',
    align: 'right',
    width: '80px',
    cell: (r) => (
      <span className={cn('num', r.alphas ? 'text-ink' : 'text-ink-subtle')}>
        {fmt.int(r.alphas)}
      </span>
    ),
  },
]

export function OperatorUsageDialog({
  usage,
  open,
  onOpenChange,
  footer,
}: {
  usage: OperatorUsage
  open: boolean
  onOpenChange: (open: boolean) => void
  footer?: ReactNode
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Operator Usage"
      description={
        <>
          How many of the <span className="num">{fmt.int(usage.alphas)}</span> Alphas you submitted
          in {usage.label} use each operator, unused first. ts_backfill and group_backfill are left
          out: BRAIN does not count them.
        </>
      }
      className="max-w-2xl"
    >
      {usage.unreadable + usage.unlisted > 0 && (
        <Notice tone="warn" title="Some submitted Alphas are not counted" className="mb-3">
          {usage.unreadable > 0 && (
            <p>
              <span className="num">{fmt.int(usage.unreadable)}</span> have an expression Alpha
              Harness could not read.
            </p>
          )}
          {usage.unlisted > 0 && (
            <p>
              <span className="num">{fmt.int(usage.unlisted)}</span> are past the most BRAIN lists
              at once.
            </p>
          )}
        </Notice>
      )}
      <DataTable
        label="Operator Usage"
        rows={usage.operators}
        columns={COLUMNS}
        rowKey={(r) => r.name}
        maxHeight="60vh"
        empty="BRAIN listed no operators."
      />
      {footer}
    </Dialog>
  )
}
