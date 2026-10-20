/**
 * Committed run events over SSE, with a paged backfill for reconnects.
 *
 * The browser's EventSource cannot send an `If-Match`/`Last-Event-ID` header, so the
 * first subscribe passes `?after=<sequence>`; a reconnect uses the last applied
 * sequence. Duplicates are expected and dropped by `(run_id, sequence)` in the store,
 * and a `stream.reset` asks the caller to reload the snapshot instead of guessing.
 */

export interface RunEventData {
  schema_version: number
  event_id: string
  run_id: string
  draft_id: string | null
  step_run_id: string | null
  attempt_id: string | null
  workflow_version_id: string | null
  sequence: number
  occurred_at: string
  status: string | null
  reason_code: string | null
  payload: Record<string, unknown>
}

export interface RunEventFrame {
  id: number
  event: string
  data: RunEventData
  /** True when the frame arrived after the run had already reached a terminal state. */
  late?: boolean
}

export interface EventHistoryPage {
  items: { id: number; event: string; data: RunEventData }[]
  next_cursor: number | null
  snapshot_url: string
}

/** Named SSE events the backend emits (`event:` frames). */
export const KNOWN_EVENT_KINDS = [
  'run.state_changed',
  'step.state_changed',
  'action.started',
  'action.finished',
  'artifact.available',
  'assistance.required',
  'budget.changed',
  'recovery.checked',
  'branch.created',
  'late_result.quarantined',
] as const

export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
}

export interface StreamHandlers {
  onEvent: (frame: RunEventFrame) => void
  onReset: (snapshotUrl: string) => void
  onError?: (error: unknown) => void
  onOpen?: () => void
}

export function parseFrame(raw: string): RunEventFrame | null {
  try {
    const data = JSON.parse(raw) as RunEventData
    if (typeof data.sequence !== 'number' || !data.run_id) return null
    return { id: data.sequence, event: String((data as unknown as { kind?: string }).kind ?? data.status ?? 'event'), data }
  } catch {
    return null
  }
}

export function streamUrl(runId: string, after?: number, baseUrl = ''): string {
  const suffix = after !== undefined && after > 0 ? `?after=${after}` : ''
  return `${baseUrl}/api/v1/runs/${runId}/events${suffix}`
}

export class RunEventStream {
  private source: EventSourceLike | null = null
  private readonly runId: string
  private readonly handlers: StreamHandlers
  private readonly baseUrl: string
  private readonly factory: ((url: string) => EventSourceLike) | null

  constructor(
    runId: string,
    handlers: StreamHandlers,
    options: { baseUrl?: string; sourceFactory?: (url: string) => EventSourceLike } = {},
  ) {
    this.runId = runId
    this.handlers = handlers
    this.baseUrl = options.baseUrl ?? ''
    this.factory = options.sourceFactory ?? null
  }

  connect(after?: number): void {
    this.close()
    const url = streamUrl(this.runId, after, this.baseUrl)
    const source = this.factory
      ? this.factory(url)
      : (new EventSource(url) as unknown as EventSourceLike)
    this.source = source

    source.addEventListener('error', () => {
      if (this.source === source) this.handlers.onError?.(new Error('event stream disconnected'))
    })
    source.addEventListener('open', () => {
      if (this.source === source) this.handlers.onOpen?.()
    })

    const handle = (kind: string) => (message: MessageEvent): void => {
      if (this.source !== source) return
      const frame = parseFrame(String(message.data))
      if (frame) this.handlers.onEvent({ ...frame, event: kind })
    }
    for (const kind of KNOWN_EVENT_KINDS) source.addEventListener(kind, handle(kind))
    source.addEventListener('stream.reset', (message: MessageEvent) => {
      if (this.source !== source) return
      let snapshotUrl = `/api/v1/runs/${this.runId}`
      try {
        const parsed = JSON.parse(String(message.data)) as { snapshot_url?: string }
        if (parsed.snapshot_url) snapshotUrl = parsed.snapshot_url
      } catch {
        // A broken reset frame still means "reload the snapshot".
      }
      this.handlers.onReset(snapshotUrl)
    })
  }

  close(): void {
    if (this.source) {
      this.source.close()
      this.source = null
    }
  }
}

export async function fetchEventHistory(
  client: { get: <T>(path: string) => Promise<T> },
  runId: string,
  after: number,
  limit = 100,
): Promise<EventHistoryPage> {
  return client.get<EventHistoryPage>(
    `/api/v1/runs/${runId}/event-history?after=${after}&limit=${limit}`,
  )
}
