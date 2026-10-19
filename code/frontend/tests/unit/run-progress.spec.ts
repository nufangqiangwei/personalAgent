import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RunProgressPage from '@/pages/RunProgressPage.vue'

const snapshot = {
  id: 'run-1',
  status: 'WAITING_USER',
  reason_code: 'MISSING_KEY_CONDITION',
  workflow_version_id: 'version-1',
  completeness: null,
  current_step_id: 'review_gaps',
  budget: {
    limits: { model_requests: 30, tokens: 100000 },
    consumed: { model_requests: 4, tokens: 1200 },
    reserved: { model_requests: 0, tokens: 0 },
  },
  artifact_ids: [],
  assistance_ids: ['assist-1'],
  allowed_actions: [
    { action: 'VIEW_ASSISTANCE', enabled: true, disabled_reason: null },
  ],
  last_event_sequence: 7,
}

const steps = {
  run_id: 'run-1',
  items: [
    {
      step_id: 'collect_official',
      status: 'SUCCEEDED',
      current_attempt_no: 1,
      skip_reason: null,
      attempts: [
        {
          attempt_no: 1,
          status: 'SUCCEEDED',
          error_category: null,
          started_at: '2026-10-19T12:00:00Z',
          ended_at: '2026-10-19T12:00:01Z',
          output: {
            directions: [
              { id: 'official', status: 'covered' },
              { id: 'ops', status: 'missing' },
            ],
          },
        },
      ],
    },
    {
      step_id: 'review_gaps',
      status: 'WAITING_USER',
      current_attempt_no: 1,
      skip_reason: null,
      attempts: [
        {
          attempt_no: 1,
          status: 'RUNNING',
          error_category: null,
          started_at: '2026-10-19T12:00:02Z',
          ended_at: null,
          output: null,
        },
      ],
    },
  ],
}

function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.endsWith('/steps') ? steps : snapshot
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('RunProgressPage', () => {
  it('shows status, budget, waiting reason and step directions', async () => {
    installFetch()
    const wrapper = mount(RunProgressPage, { props: { id: 'run-1' } })
    await flushPromises()

    expect(wrapper.text()).toContain('WAITING_USER')
    expect(wrapper.text()).toContain('MISSING_KEY_CONDITION')
    expect(wrapper.text()).toContain('模型 4 / 30')
    expect(wrapper.text()).toContain('collect_official')
    expect(wrapper.text()).toContain('official：covered')
    expect(wrapper.text()).toContain('ops：missing')
    expect(wrapper.text()).toContain('不会自动继续')
  })

  it('lists artifacts when the run produced a document', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        const body = url.endsWith('/steps')
          ? { run_id: 'run-1', items: [] }
          : { ...snapshot, status: 'SUCCEEDED', completeness: 'COMPLETE', artifact_ids: ['a1'] }
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }),
    )
    const wrapper = mount(RunProgressPage, {
      props: { id: 'run-1' },
      global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('查看文档')
    expect(wrapper.text()).toContain('COMPLETE')
  })

  it('surfaces an API error without breaking the page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: {
                code: 'RUN_NOT_FOUND',
                message: 'missing',
                retryable: false,
                details: {},
                request_id: 'r1',
              },
            }),
            { status: 404, headers: { 'content-type': 'application/json' } },
          ),
      ),
    )
    const wrapper = mount(RunProgressPage, { props: { id: 'missing' } })
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  })
})
