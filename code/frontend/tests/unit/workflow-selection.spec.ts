import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import WorkflowDetailPage from '@/pages/WorkflowDetailPage.vue'
import WorkflowListPage from '@/pages/WorkflowListPage.vue'

const Empty = { template: '<div />' }

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'workflows', component: Empty },
      { path: '/workflows/:id', name: 'workflow-detail', component: Empty },
      { path: '/requirements/:id', name: 'requirements', component: Empty },
      { path: '/workflows/:id/versions', name: 'workflow-versions', component: Empty },
    ],
  })
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const workflow = {
  id: 'aaaaaaaa-1111-4111-8111-111111111111',
  name: '技术选型对比',
  description: '比较具体软件产品。',
  applicability: '需要比较两个以上具体产品时使用。',
  disabled: false,
  revision: 2,
  latest_version: { id: 'version-1', version_no: 1, definition_hash: 'sha256:abc' },
  directions: [
    { id: 'official', title: '官方能力', scope: '官方文档' },
    { id: 'ops', title: '运维与成本限制', scope: '部署与成本' },
  ],
  required_inputs: ['topic'],
  steps: [
    { id: 'collect_official', name: '收集官方能力证据', type: 'AgentTask' },
    { id: 'write_comparison', name: '生成对比报告', type: 'Output' },
  ],
  output: { structure: 'comparison' },
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('WorkflowListPage', () => {
  it('lists workflows without requiring any initial input', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ items: [workflow], next_cursor: null })),
    )
    const router = makeRouter()
    await router.push('/')
    const wrapper = mount(WorkflowListPage, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.find('input').exists()).toBe(false)
    expect(wrapper.text()).toContain('技术选型对比')
    expect(wrapper.text()).toContain('需要比较两个以上具体产品时使用。')
    expect(wrapper.text()).toContain('官方能力')
    expect(wrapper.text()).toContain('v1')
  })

  it('routes to the requirements page with the exact selected version', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ items: [workflow], next_cursor: null })),
    )
    const router = makeRouter()
    await router.push('/')
    const wrapper = mount(WorkflowListPage, { global: { plugins: [router] } })
    await flushPromises()

    const select = wrapper
      .findAll('button')
      .find((button) => button.text().includes('选择该流程'))
    expect(select).toBeDefined()
    await select?.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('requirements')
    expect(router.currentRoute.value.query.workflow_version_id).toBe('version-1')
  })

  it('explains an empty library without inventing or auto-matching', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ items: [], next_cursor: null })))
    const router = makeRouter()
    await router.push('/')
    const wrapper = mount(WorkflowListPage, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.text()).toContain('当前没有可用的固定流程')
    expect(wrapper.text()).toContain('不会自动匹配或进入探索')
  })

  it('surfaces a load error and keeps the page usable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: 'INTERNAL_ERROR',
              message: 'boom',
              retryable: false,
              details: {},
              request_id: 'r1',
            },
          },
          500,
        ),
      ),
    )
    const router = makeRouter()
    await router.push('/')
    const wrapper = mount(WorkflowListPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('服务发生内部错误')
  })
})

describe('WorkflowDetailPage', () => {
  it('shows applicability, inputs, directions and steps', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(workflow)))
    const router = makeRouter()
    await router.push(`/workflows/${workflow.id}`)
    const wrapper = mount(WorkflowDetailPage, {
      props: { id: workflow.id },
      global: { plugins: [router] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('适用边界')
    expect(wrapper.text()).toContain('topic')
    expect(wrapper.text()).toContain('收集官方能力证据')
    expect(wrapper.text()).toContain('comparison')
  })

  it('disables selection for a disabled workflow', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ...workflow, disabled: true })))
    const router = makeRouter()
    await router.push(`/workflows/${workflow.id}`)
    const wrapper = mount(WorkflowDetailPage, {
      props: { id: workflow.id },
      global: { plugins: [router] },
    })
    await flushPromises()

    const select = wrapper
      .findAll('button')
      .find((button) => button.text().includes('选择该流程'))
    expect(select?.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('已停用')
  })
})
