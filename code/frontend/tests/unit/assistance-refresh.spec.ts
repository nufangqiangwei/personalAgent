import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'

import AssistancePanel from '@/components/AssistancePanel.vue'
import { createRunStore, type RunSnapshot } from '@/stores/run'

afterEach(() => vi.unstubAllGlobals())

it('loads a new assistance request when committed events refresh the run snapshot', async () => {
  let pending = false
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: pending ? [{
    id: 'a1', run_id: 'run-1', kind: 'BUDGET_LIMIT', status: 'OPEN', revision: 1,
    limit: { model_requests: 1 }, consumed: { model_requests: 1 },
    completed_steps: ['collect'], remaining_steps: ['write'], observed_problem: '研究额度已到限',
    unknowns: [], attempted_remedies: [], decision_schema: {}, available_decisions: ['DISCUSS'],
    decisions: [], created_at: '2026-10-20T00:00:00Z',
  }] : [] }), { headers: { 'content-type': 'application/json' } })))
  const store = createRunStore('run-1')
  const snapshot = { id: 'run-1', status: 'RUNNING', assistance_ids: [],
    last_event_sequence: 1, epoch: 1, revision: 1 } as unknown as RunSnapshot
  store.applySnapshot(snapshot)
  const wrapper = mount(AssistancePanel, { props: { runId: 'run-1', store } })
  await flushPromises()
  expect(wrapper.text()).toContain('当前没有待处理的求助')
  pending = true
  store.applySnapshot({ ...snapshot, status: 'WAITING_USER', assistance_ids: ['a1'], revision: 2 })
  await flushPromises()
  expect(wrapper.text()).toContain('研究额度已到限')
  expect(wrapper.text()).toContain('BUDGET_LIMIT')
  wrapper.unmount()
})
