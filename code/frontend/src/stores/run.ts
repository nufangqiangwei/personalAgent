/**
 * Run state shared by the progress page and its controls.
 *
 * Server facts win: buttons are enabled only from `allowed_actions`, the snapshot's
 * `draining`/`in_flight_actions` are shown verbatim, and progress arrives as
 * committed events. Events are deduplicated by `(run_id, sequence)`, a reconnect
 * backfills from the last applied sequence, and `stream.reset` forces a fresh
 * snapshot instead of silently continuing with a gap.
 */

import { computed, reactive } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'
import { RunEventStream, fetchEventHistory, streamUrl } from '@/api/events'
import type { EventSourceLike, RunEventFrame } from '@/api/events'

export interface AllowedAction {
  action: string
  enabled: boolean
  disabled_reason: string | null
}

export interface RunSnapshot {
  id: string
  root_run_id: string
  parent_run_id: string | null
  fork_step_id: string | null
  status: string
  reason_code: string | null
  revision: number
  epoch: number
  workflow_version_id: string
  current_step_id: string | null
  completeness: string | null
  draining: boolean
  in_flight_actions: { id: string; type: string; status: string; step_run_id?: string }[]
  assistance_ids: string[]
  artifact_ids: string[]
  budget: {
    limits: Record<string, number>
    consumed: Record<string, number>
    reserved: Record<string, number>
  }
  last_event_sequence: number
  allowed_actions: AllowedAction[]
  updated_at: string
}

export interface StepAttempt {
  attempt_no: number
  status: string
  error_category: string | null
  started_at: string
  ended_at: string | null
  output: Record<string, unknown> | null
}

export interface StepView {
  step_id: string
  status: string
  current_attempt_no: number
  selected_transition: string | null
  skip_reason: string | null
  attempts: StepAttempt[]
}

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED'])

export class RunStore {
  readonly state = reactive({
    run: null as RunSnapshot | null,
    steps: [] as StepView[],
    events: [] as RunEventFrame[],
    error: null as string | null,
    loading: true,
    resetRequired: false,
    /** When the snapshot first recorded a terminal status. */
    terminalAt: null as string | null,
    /** Events that arrived after the run was already terminal. */
    lateEvents: [] as RunEventFrame[],
  })

  private applied = new Set<string>()
  private stream: RunEventStream | null = null
  private sequence: number | null = null
  private floor = 0
  private connectionOptions: { sourceFactory?: (url: string) => EventSourceLike } = {}
  private generation = 0
  private refreshing: Promise<void> | null = null
  private refreshAgain = false
  private readonly client: ApiClient
  private readonly runId: string

  constructor(runId: string, client: ApiClient = new ApiClient()) {
    this.runId = runId
    this.client = client
  }

  readonly terminal = computed(() => TERMINAL.has(this.state.run?.status ?? ''))
  readonly draining = computed(() => this.state.run?.draining ?? false)
  readonly inFlight = computed(() => this.state.run?.in_flight_actions ?? [])
  readonly canPause = computed(() => this.action('PAUSE'))
  readonly canCancel = computed(() => this.action('CANCEL'))
  readonly canResume = computed(() => this.action('RESUME'))

  action(name: string): AllowedAction | undefined {
    return this.state.run?.allowed_actions.find((item) => item.action === name)
  }

  /** Last sequence applied to the UI, used for reconnects and backfill. */
  get lastSequence(): number {
    return this.sequence ?? 0
  }

  async load(): Promise<void> {
    const generation = this.generation
    try {
      const snapshot = await this.client.get<RunSnapshot>(`/api/v1/runs/${this.runId}`)
      const steps = (
        await this.client.get<{ items: StepView[] }>(`/api/v1/runs/${this.runId}/steps`)
      ).items
      if (generation !== this.generation) return
      this.applySnapshot(snapshot, steps)
      this.state.error = null
    } catch (caught) {
      this.state.error = describe(caught)
    } finally {
      this.state.loading = false
    }
  }

  applySnapshot(snapshot: RunSnapshot, steps: StepView[] = this.state.steps): void {
    if (this.state.run && snapshot.revision < this.state.run.revision) return
    if (this.sequence === null) {
      this.sequence = snapshot.last_event_sequence
      this.floor = this.sequence
    }
    this.state.run = snapshot
    if (TERMINAL.has(snapshot.status)) {
      this.state.terminalAt = snapshot.updated_at
    }
    this.state.steps = steps
  }

  /** Apply committed events, dropping any duplicate `(run_id, sequence)`. */
  applyEvents(frames: RunEventFrame[]): RunEventFrame[] {
    const accepted: RunEventFrame[] = []
    for (const frame of frames) {
      if (frame.data.run_id !== this.runId || frame.data.sequence <= this.floor) continue
      const key = `${frame.data.run_id}:${frame.data.sequence}`
      if (this.applied.has(key)) continue
      this.applied.add(key)
      const late =
        this.state.terminalAt !== null &&
        frame.data.occurred_at > this.state.terminalAt
      const stamped: RunEventFrame = late ? { ...frame, late: true } : frame
      accepted.push(stamped)
      this.state.events.push(stamped)
      if (late) this.state.lateEvents.push(stamped)
    }
    this.state.events.sort((left, right) => left.data.sequence - right.data.sequence)
    while (this.applied.has(`${this.runId}:${this.lastSequence + 1}`)) {
      this.sequence = this.lastSequence + 1
    }
    return accepted
  }

  async backfill(): Promise<RunEventFrame[]> {
    const generation = this.generation
    let cursor = this.lastSequence
    const accepted: RunEventFrame[] = []
    try {
      for (;;) {
        const page = await fetchEventHistory(this.client, this.runId, cursor)
        if (generation !== this.generation) return accepted
        accepted.push(...this.applyEvents(page.items))
        if (page.next_cursor === null) break
        if (page.next_cursor <= cursor) throw new Error('event cursor did not advance')
        cursor = page.next_cursor
      }
      return accepted
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'EVENT_CURSOR_EXPIRED') {
        await this.handleReset()
        return []
      }
      this.state.error = describe(caught)
      throw caught
    }
  }

  private refresh(): Promise<void> {
    this.refreshAgain = true
    if (!this.refreshing) {
      this.refreshing = (async () => {
        while (this.refreshAgain) {
          this.refreshAgain = false
          await this.load()
        }
      })().finally(() => { this.refreshing = null })
    }
    return this.refreshing
  }

  connect(options: { sourceFactory?: (url: string) => EventSourceLike } = {}): void {
    this.connectionOptions = options
    this.stream?.close()
    const streamOptions: { sourceFactory?: (url: string) => EventSourceLike } = {}
    if (options.sourceFactory) streamOptions.sourceFactory = options.sourceFactory
    this.stream = new RunEventStream(
      this.runId,
      {
        onEvent: (frame) => {
          if (this.applyEvents([frame]).length > 0) void this.refresh()
        },
        onReset: () => {
          void this.handleReset()
        },
        onError: (error) => {
          this.state.error = describe(error)
        },
        onOpen: () => {
          void this.backfill().then(() => this.refresh()).catch(() => undefined)
        },
      },
      streamOptions,
    )
    this.stream.connect(this.lastSequence)
  }

  async handleReset(): Promise<void> {
    this.stream?.close()
    this.generation += 1
    this.state.resetRequired = true
    this.applied.clear()
    this.state.events = []
    this.state.lateEvents = []
    this.sequence = null
    await this.load()
    if (!this.state.error) this.connect(this.connectionOptions)
  }

  disconnect(): void {
    this.generation += 1
    this.stream?.close()
    this.stream = null
  }

  streamLink(after?: number): string {
    return streamUrl(this.runId, after)
  }
}

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '读取失败。'
}

export function createRunStore(runId: string): RunStore {
  return new RunStore(runId)
}
