import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import WorkflowDefinitionEditor from '@/components/WorkflowDefinitionEditor.vue'

const definition = {
  schema_version: '1',
  workflow_id: '11111111-2222-4333-8444-555555555555',
  version: 1,
  name: '技术选型对比',
  steps: [{ id: 'collect', type: 'AgentTask' }],
}

function mountEditor(errorMessage: string | null = null) {
  return mount(WorkflowDefinitionEditor, {
    props: {
      versionId: 'v1',
      definition,
      saving: false,
      errorMessage,
    },
  })
}

describe('WorkflowDefinitionEditor', () => {
  it('shows the definition as formatted JSON', () => {
    const wrapper = mountEditor()
    const textarea = wrapper.find('textarea')
    expect(textarea.element.value).toContain('"技术选型对比"')
    expect(JSON.parse(textarea.element.value)).toEqual(definition)
  })

  it('blocks saving when the JSON is invalid and explains why', async () => {
    const wrapper = mountEditor()
    await wrapper.find('textarea').setValue('{ not json')
    expect(wrapper.text()).toContain('JSON 解析失败')
    expect(wrapper.find('button').attributes('disabled')).toBeDefined()
    expect(wrapper.emitted('save')).toBeUndefined()
  })

  it('requires a non-empty change summary', async () => {
    const wrapper = mountEditor()
    const button = wrapper.find('button')
    expect(button.attributes('disabled')).toBeDefined()
    await wrapper.find('#change-summary').setValue('调整定义')
    expect(wrapper.find('button').attributes('disabled')).toBeUndefined()
  })

  it('emits the parsed definition and the executable confirmation', async () => {
    const wrapper = mountEditor()
    await wrapper.find('#change-summary').setValue('调整定义')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.find('button').trigger('click')
    const emitted = wrapper.emitted('save')
    expect(emitted).toHaveLength(1)
    const payload = emitted?.[0]?.[0] as {
      definition: Record<string, unknown>
      changeSummary: string
      confirmExecutable: boolean
    }
    expect(payload.definition).toEqual(definition)
    expect(payload.changeSummary).toBe('调整定义')
    expect(payload.confirmExecutable).toBe(true)
  })

  it('keeps the local text while showing a server conflict message', async () => {
    const wrapper = mountEditor('工作流版本已变化，请重新选择并确认。')
    await wrapper.find('textarea').setValue(JSON.stringify({ ...definition, name: '本地修改' }))
    expect(wrapper.find('textarea').element.value).toContain('本地修改')
    expect(wrapper.text()).toContain('工作流版本已变化')
  })
})
