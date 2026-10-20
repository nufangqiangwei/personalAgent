import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'

import BranchProposalPanel from '@/components/BranchProposalPanel.vue'

const proposal = {
  id: 'p1',
  parent_run_id: 'run-1',
  root_run_id: 'run-1',
  selected_step_id: 's2',
  effective_step_id: 's2',
  draft_id: 'd1',
  source_draft_id: 'd0',
  status: 'READY',
  revision: 2,
  changed_paths: ['fields/topic'],
  input_reads_closure: [],
  affected_step_ids: ['s2'],
  reusable_step_ids: ['s1'],
  invalidated_artifact_ids: [],
  blocked_reasons: [],
  report_hash: 'sha256:report',
  confirmation_id: null,
  created_run_id: null,
}

const analysis = {
  status: 'READY',
  changed_paths: ['fields/topic'],
  affected_step_ids: ['s2'],
  reusable_step_ids: ['s1'],
  invalidated_artifact_ids: [],
  blocked_reasons: [],
  suggested_start_step_id: null,
  report_hash: 'sha256:report',
}

function respond(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

async function mountPanel() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/runs', component: { template: '<div />' } },
      { path: '/requirements/:id', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(BranchProposalPanel, {
    props: {
      parentRunId: 'run-1',
      steps: [{ step_id: 's2', status: 'SUCCEEDED' }],
      proposalId: 'p1',
    },
    global: {
      plugins: [router],
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  })
  await flushPromises()
  return wrapper
}

describe('BranchProposalPanel', () => {
  it('shows the full before/after requirements and needs an explicit confirmation', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/validate')) return respond({ proposal, analysis })
      if (url.includes('/requirements/d0')) {
        return respond({ summary_markdown: 'BEFORE-REQUIREMENTS', confirmation_id: null })
      }
      if (url.includes('/requirements/d1')) {
        return respond({ summary_markdown: 'AFTER-REQUIREMENTS', confirmation_id: 'c1' })
      }
      if (url.endsWith('/branch-proposals/p1/confirm')) return respond({ id: 'run-2' })
      if (url.endsWith('/branch-proposals/p1')) return respond(proposal)
      return respond({})
    }))

    const wrapper = await mountPanel()
    expect(wrapper.text()).toContain('READY')
    expect(wrapper.text()).toContain('s2')

    const validateButton = wrapper.findAll('button').find((item) => item.text().includes('校验影响范围'))
    expect(validateButton).toBeTruthy()
    await validateButton!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('修改前要求')
    expect(wrapper.text()).toContain('BEFORE-REQUIREMENTS')
    expect(wrapper.text()).toContain('修改后要求')
    expect(wrapper.text()).toContain('AFTER-REQUIREMENTS')

    const confirmButton = wrapper.findAll('button').find((item) => item.text().includes('建立分支'))
    expect(confirmButton).toBeTruthy()
    await confirmButton!.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('created')?.[0]).toEqual(['run-2'])
  })
})
