/**
 * The one way the frontend talks to the local backend. The backend answers errors in three
 * different shapes, and every one is folded into an `ApiError` carrying a stable `code` and a
 * message fit to show.
 */

export interface ApiErrorBody {
  code: string
  message: string
  retryable?: boolean
  retryAfter?: number
  verificationUrl?: string
  [extra: string]: unknown
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly body: ApiErrorBody

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.body = body
  }

  get retryable(): boolean {
    return this.body.retryable ?? this.status >= 500
  }
}

function describe(status: number): string {
  if (status === 0)
    return import.meta.env.DEV
      ? 'Cannot reach the Alpha Harness backend. Start it on port 8000 and try again.'
      : 'Cannot reach Alpha Harness: it is not running. Open it again.'
  if (status >= 500) return 'The backend failed while handling that request.'
  return `The request was refused (${status}).`
}

const text = (value: unknown): string | undefined =>
  typeof value === 'string' && value.trim() ? value.trim() : undefined

/** BRAIN's own wording often sits in `detail` or `fields.detail`; it is the useful part. */
function fromObject(status: number, o: Record<string, unknown>): ApiErrorBody {
  const fields = o['fields'] as Record<string, unknown> | undefined
  const fieldDetail = Array.isArray(fields?.['detail'])
    ? (fields['detail'] as unknown[]).map(String).join(' ')
    : text(fields?.['detail'])
  const base = text(o['message']) ?? describe(status)
  const extra = text(o['detail']) ?? fieldDetail
  return {
    ...o,
    code: text(o['code']) ?? `http_${status}`,
    message: extra && !base.includes(extra) ? `${base} ${extra}` : base,
  }
}

export function normalise(status: number, raw: unknown): ApiErrorBody {
  if (raw && typeof raw === 'object') {
    const detail = (raw as Record<string, unknown>)['detail']
    if (Array.isArray(detail)) {
      const message = detail
        .map((d) => {
          const e = d as { loc?: unknown[]; msg?: string }
          const where = (e.loc ?? [])
            .filter((part) => part !== 'body' && part !== 'query')
            .join('.')
          return where ? `${where}: ${e.msg}` : String(e.msg)
        })
        .join('; ')
      return {
        code: 'invalid_request',
        message: message || describe(status),
      }
    }
    if (detail && typeof detail === 'object')
      return fromObject(status, detail as Record<string, unknown>)
    const message = text(detail)
    if (message) return { code: `http_${status}`, message }
  }
  const message = text(raw)
  if (message) return { code: `http_${status}`, message: message.slice(0, 300) }
  return { code: `http_${status}`, message: describe(status) }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  // Serialised before the request: a body that cannot be turned into JSON throws a TypeError
  // too, which inside the fetch below would read as an unreachable backend.
  const payload = body === undefined ? null : JSON.stringify(body)
  let response: Response
  try {
    response = await fetch(path, {
      method,
      // A query superseded by a newer one is dropped here rather than read to its end.
      ...(signal ? { signal } : {}),
      // X-Harness-Client: the backend refuses writes without it (cross-site forms can't set it).
      headers: {
        'X-Harness-Client': '1',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: payload,
    })
  } catch (failure) {
    // Only a failed fetch is a TypeError; anything else, a cancelled request's AbortError
    // included, must not be reported as "the backend is not running".
    if (!(failure instanceof TypeError)) throw failure
    throw new ApiError(0, {
      code: 'backend_unreachable',
      message: describe(0),
    })
  }

  if (response.status === 204) return undefined as T

  const raw = await response.text()
  let parsed: unknown = raw
  if (raw) {
    try {
      parsed = JSON.parse(raw)
    } catch {
      // Plain text: an unhandled 500, kept as-is for the message.
    }
  }

  if (!response.ok) throw new ApiError(response.status, normalise(response.status, parsed))
  return parsed as T
}

export const http = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>('GET', path, undefined, signal),
  /** Bodies default to `{}`: several routes require a body even when every field is optional. */
  post: <T>(path: string, body: unknown = {}, signal?: AbortSignal) =>
    request<T>('POST', path, body, signal),
  put: <T>(path: string, body: unknown = {}) => request<T>('PUT', path, body),
  patch: <T>(path: string, body: unknown = {}) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}

/** `?a=1&b=x` from the defined entries, or an empty string. */
export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== '',
  )
  return entries.length
    ? `?${new URLSearchParams(entries.map(([k, v]): [string, string] => [k, String(v)])).toString()}`
    : ''
}

/** A message for a toast or an Alert, from anything a query or mutation threw. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}
