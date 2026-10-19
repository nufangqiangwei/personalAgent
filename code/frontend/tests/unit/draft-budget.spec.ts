import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftBudgetPanel from '@/components/DraftBudgetPanel.vue'

interface Call {
  url: string
  method: string
  headers: Record<string, string>
  body: unknown
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function budget(overrides: Record<string, unknown> = {}) {
  return {
    draft_id: 'draft-1',
    ledger_id: 'ledger-1',
    ledger_scope: 'DRAFT',
    revision: 4,
    limits: { model_requests: 20, tokens: 30000 },
    consumed: { model_requests: 20, tokens: 28750 },
    reserved: { model_requests: 0, tokens: 0 },
    remaining: { model_requests: 0, tokens: 1250 },
    active_ms: 0,
    exhausted: true,
    exhausted_dimensions: ['model_requests'],
    assistance: {
      kind: 'BUDGET_LIMIT',
      status: 'OPEN',
      observed_problem: '草稿模型请求已达上限；尚未创建研究run。',
      unknown_model_request: false,
      available_actions: [
        { action: 'GRANT_DRAFT_BUDGET', enabled: true, disabled_reason: null },
      ],
    },
    updated_at: '2026-10-19T12:00:00Z',
    ...overrides,
  }
}

function installFetch(responses: ((call: Call) => Response)[]) {
  const calls: Call[] = []
  const queue = [...responses]
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        url: String(input),
        method: (init?.method ?? 'GET').toUpperCase(),
        headers: (init?.headers ?? {}) as Record<string, string>,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      }
      calls.push(call)
      const handler = queue.length > 1 ? queue.shift() : queue[0]
      if (!handler) throw new Error('no response configured')
      return handler(call)
    }),
  )
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('DraftBudgetPanel', () => {
  it('shows consumed, limits and the exhausted dimension', async () => {
    installFetch([() => jsonResponse(budget())])
    const wrapper = mount(DraftBudgetPanel, {
      props: { draftId: 'draft-1', draftRevision: 4, draftStatus: 'COLLECTING' },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('20 / 20')
    expect(wrapper.text()).toContain('28750 / 30000')
    expect(wrapper.text()).toContain('已触顶维度：model_requests')
    expect(wrapper.text()).toContain('草稿模型请求已达上限')
  })

  it('does not offer a top-up when the ledger still has room', async () => {
    installFetch([
      () =>
        jsonResponse(
          budget({
            consumed: { model_requests: 2, tokens: 1000 },
            remaining: { model_requests: 18, tokens: 29000 },
            exhausted: false,
            exhausted_dimensions: [],
            assistance: null,
          }),
        ),
    ])
    const wrapper = mount(DraftBudgetPanel, {
      props: { draftId: 'draft-1', draftRevision: 2, draftStatus: 'READY' },
    })
    await flushPromises()
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).toContain('不会创建执行或确认')
  })

  it('grants explicitly with If-Match and refreshes the snapshot', async () => {
    const calls = installFetch([
      () => jsonResponse(budget()),
      () =>
        jsonResponse({
          grant_id: 'grant-1',
          ledger_scope: 'DRAFT',
          research_started: false,
          confirmation_id: null,
          run_id: null,
        }, 201),
      () =>
        jsonResponse(
          budget({
            revision: 5,
            limits: { model_requests: 25, tokens: 35000 },
            remaining: { model_requests: 5, tokens: 6250 },
            exhausted: false,
            exhausted_dimensions: [],
            assistance: null,
          }),
        ),
    ])
    const wrapper = mount(DraftBudgetPanel, {
      props: { draftId: 'draft-1', draftRevision: 4, draftStatus: 'COLLECTING' },
    })
    await flushPromises()

    const button = wrapper.findAll('button').find((item) => item.text().includes('明确追加额度'))
    expect(button).toBeDefined()
    await button?.trigger('click')
    await flushPromises()

    const post = calls.find((call) => call.method === 'POST')
    expect(post?.url).toBe('/api/v1/requirements/draft-1/budget-grants')
    expect(post?.headers['If-Match']).toBe('"4"')
    expect(post?.headers['Idempotency-Key']).toBeTruthy()
    expect(post?.body).toMatchObject({
      draft_revision: 4,
      approved_increment: { model_requests: 5, tokens: 5000 },
      explicit_grant: true,
      acknowledge_cost_and_ledger: true,
    })
    expect(wrapper.emitted('granted')).toHaveLength(1)
    expect(wrapper.text()).toContain('已追加草稿额度')
    expect(wrapper.text()).toContain('25')
  })

  it('disables the top-up for a confirmed draft and explains why', async () => {
    installFetch([() => jsonResponse(budget())])
    const wrapper = mount(DraftBudgetPanel, {
      props: { draftId: 'draft-1', draftRevision: 9, draftStatus: 'CONFIRMED' },
    })
    await flushPromises()

    const button = wrapper.findAll('button').find((item) => item.text().includes('明确追加额度'))
    expect(button?.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('不允许追加')
  })

  it('surfaces a conflict error without inventing a new limit', async () => {
    installFetch([
      () => jsonResponse(budget()),
      () =>
        jsonResponse(
          {
            error: {
              code: 'DRAFT_BUDGET_CONFLICT',
              message: 'conflict',
              retryable: false,
              details: {},
              request_id: 'r1',
            },
          },
          409,
        ),
    ])
    const wrapper = mount(DraftBudgetPanel, {
      props: { draftId: 'draft-1', draftRevision: 4, draftStatus: 'COLLECTING' },
    })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((item) => item.text().includes('明确追加额度'))
      ?.trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('请重新读取')
  })
})
