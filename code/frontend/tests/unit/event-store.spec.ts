import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RunEventData } from '@/api/events'
import type { EventSourceLike } from '@/api/events'
import { RunStore, type RunSnapshot } from '@/stores/run'

function snapshot(overrides: Partial<RunSnapshot> = {}): RunSnapshot {
  return {
    id: 'run-1',
    root_run_id: 'run-1',
    parent_run_id: null,
    fork_step_id: null,
    status: 'RUNNING',
    reason_code: null,
    revision: 3,
    epoch: 1,
    workflow_version_id: 'version-1',
    current_step_id: 'collect',
    completeness: null,
    draining: false,
    in_flight_actions: [],
    assistance_ids: [],
    artifact_ids: [],
    budget: { limits: { tokens: 100 }, consumed: { tokens: 10 }, reserved: {} },
    last_event_sequence: 4,
    allowed_actions: [{ action: 'PAUSE', enabled: true, disabled_reason: null }],
    updated_at: '2026-10-19T12:00:05.000000Z',
    ...overrides,
  }
}

function event(sequence: number, overrides: Partial<RunEventData> = {}) {
  const data: RunEventData = {
    schema_version: 1,
    event_id: `event-${sequence}`,
    run_id: 'run-1',
    draft_id: null,
    step_run_id: null,
    attempt_id: null,
    workflow_version_id: 'version-1',
    sequence,
    occurred_at: '2026-10-19T12:00:00.000000Z',
    status: 'RUNNING',
    reason_code: null,
    payload: {},
    ...overrides,
  }
  return { id: sequence, event: 'run.state_changed', data }
}

class FakeClient {
  calls: string[] = []
  responses = new Map<string, unknown>()

  async get<T>(path: string): Promise<T> {
    this.calls.push(path)
    if (!this.responses.has(path)) throw new Error(`no response for ${path}`)
    return this.responses.get(path) as T
  }
}

class FakeSource implements EventSourceLike {
  listeners = new Map<string, ((event: MessageEvent) => void)[]>()
  closed = false

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    const current = this.listeners.get(type) ?? []
    current.push(listener)
    this.listeners.set(type, current)
  }

  close(): void {
    this.closed = true
  }

  emit(type: string, data: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(data) } as MessageEvent)
    }
  }
}

describe('run event store', () => {
  let client: FakeClient
  let store: RunStore

  beforeEach(() => {
    client = new FakeClient()
    store = new RunStore('run-1', client as never)
    store.applySnapshot(snapshot())
  })

  it('drops duplicate (run_id, sequence) frames', () => {
    const frame = event(5)
    expect(store.applyEvents([frame])).toHaveLength(1)
    expect(store.applyEvents([frame])).toHaveLength(0)
    expect(store.applyEvents([event(5, { run_id: 'run-1' })])).toHaveLength(0)
    expect(store.state.events).toHaveLength(1)
  })

  it('backfills from the last applied sequence after a disconnect', async () => {
    client.responses.set('/api/v1/runs/run-1/event-history?after=4&limit=100', {
      items: [event(5), event(6)],
      next_cursor: null,
      snapshot_url: '/api/v1/runs/run-1',
    })
    const accepted = await store.backfill()
    expect(client.calls).toEqual(['/api/v1/runs/run-1/event-history?after=4&limit=100'])
    expect(accepted.map((frame) => frame.data.sequence)).toEqual([5, 6])
    expect(store.lastSequence).toBe(6)
  })

  it('reloads the snapshot on stream.reset instead of guessing the gap', async () => {
    client.responses.set('/api/v1/runs/run-1', snapshot({ last_event_sequence: 9 }))
    client.responses.set('/api/v1/runs/run-1/steps', { items: [] })
    client.responses.set('/api/v1/runs/run-1/event-history?after=9&limit=100', {
      items: [], next_cursor: null,
    })
    store.applyEvents([event(5)])
    expect(store.state.events).toHaveLength(1)

    const sources: FakeSource[] = []
    const urls: string[] = []
    store.connect({ sourceFactory: (url) => {
      urls.push(url)
      const source = new FakeSource()
      sources.push(source)
      return source
    } })
    const source = sources[0]!
    source.emit('stream.reset', { snapshot_url: '/api/v1/runs/run-1' })
    await vi.waitFor(() => {
      expect(store.state.run?.last_event_sequence).toBe(9)
      expect(sources).toHaveLength(2)
    })

    expect(store.state.events).toHaveLength(0)
    expect(store.state.run?.last_event_sequence).toBe(9)
    expect(client.calls).toContain('/api/v1/runs/run-1')
    expect(source.closed).toBe(true)
    expect(urls.at(-1)).toContain('after=9')
    expect(sources).toHaveLength(2)
    // Frames buffered by the old subscription cannot reintroduce the cleaned gap.
    expect(store.applyEvents([event(5)])).toHaveLength(0)
  })

  it('drains all history pages using an independent advancing cursor', async () => {
    for (const [after, sequences, next] of [[4, [5, 6], 6], [6, [7, 8], 8], [8, [9], null]] as const) {
      client.responses.set(`/api/v1/runs/run-1/event-history?after=${after}&limit=100`, {
        items: sequences.map((sequence) => event(sequence)), next_cursor: next,
      })
    }
    expect((await store.backfill()).map((frame) => frame.id)).toEqual([5, 6, 7, 8, 9])
    expect(store.lastSequence).toBe(9)
    store.applySnapshot(snapshot({ last_event_sequence: 12 }))
    expect(store.lastSequence).toBe(9) // A newer snapshot must not skip unseen frames.
  })

  it('refreshes committed state, steps and artifacts immediately on stream events', async () => {
    client.responses.set('/api/v1/runs/run-1', snapshot({
      status: 'SUCCEEDED', last_event_sequence: 5, artifact_ids: ['document-1'],
      allowed_actions: [{ action: 'VIEW_ARTIFACTS', enabled: true, disabled_reason: null }],
    }))
    client.responses.set('/api/v1/runs/run-1/steps', { items: [] })
    const source = new FakeSource()
    store.connect({ sourceFactory: () => source })
    source.emit('artifact.available', event(5).data)
    await vi.waitFor(() => expect(store.state.run?.artifact_ids).toEqual(['document-1']))
    expect(store.state.run?.status).toBe('SUCCEEDED')
    expect(store.canPause.value?.enabled).not.toBe(true)
    expect(store.lastSequence).toBe(5)
  })

  it('reports disconnects and backfills when the connection reopens', async () => {
    client.responses.set('/api/v1/runs/run-1/event-history?after=4&limit=100', {
      items: [event(5)], next_cursor: null,
    })
    client.responses.set('/api/v1/runs/run-1', snapshot({ last_event_sequence: 5 }))
    client.responses.set('/api/v1/runs/run-1/steps', { items: [] })
    const source = new FakeSource()
    store.connect({ sourceFactory: () => source })
    source.emit('error', {})
    expect(store.state.error).toBeTruthy()
    source.emit('open', {})
    await vi.waitFor(() => expect(store.lastSequence).toBe(5))
    expect(store.state.events.map((frame) => frame.id)).toEqual([5])
  })

  it('shows late events without turning them into success', () => {
    store.applySnapshot(
      snapshot({ status: 'SUCCEEDED', updated_at: '2026-10-19T12:00:10.000000Z' }),
    )
    const late = event(11, {
      status: 'RUNNING',
      occurred_at: '2026-10-19T12:00:20.000000Z',
    })
    const accepted = store.applyEvents([late])
    expect(accepted[0].late).toBe(true)
    expect(store.state.lateEvents).toHaveLength(1)
    expect(store.state.run?.status).toBe('SUCCEEDED')
  })

  it('keeps draining and in-flight facts from the server snapshot', () => {
    store.applySnapshot(
      snapshot({
        status: 'PAUSING',
        draining: true,
        in_flight_actions: [
          { id: 'attempt-1', type: 'model_request', status: 'RUNNING' },
        ],
        allowed_actions: [
          { action: 'PAUSE', enabled: false, disabled_reason: '执行正在收敛' },
          { action: 'CANCEL', enabled: true, disabled_reason: null },
        ],
      }),
    )
    expect(store.draining.value).toBe(true)
    expect(store.inFlight.value).toHaveLength(1)
    expect(store.canPause.value?.enabled).toBe(false)
    expect(store.canCancel.value?.enabled).toBe(true)
  })
})
