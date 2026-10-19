<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface WorkflowDetail {
  id: string
  name: string
  description: string
  applicability: string
  disabled: boolean
  latest_version: { id: string; version_no: number; definition_hash: string } | null
  directions: { id: string; title: string; scope: string }[]
  required_inputs: string[]
  steps: { id: string; name: string; type: string }[]
  output: { structure: string | null }
}

const props = defineProps<{ id: string }>()
const client = new ApiClient()
const router = useRouter()

const workflow = ref<WorkflowDetail | null>(null)
const error = ref<string | null>(null)
const loading = ref(true)

onMounted(async () => {
  try {
    workflow.value = await client.get<WorkflowDetail>(`/api/v1/workflows/${props.id}`)
  } catch (caught) {
    error.value = caught instanceof ApiError ? messageFor(caught.code, caught.message) : '加载失败。'
  } finally {
    loading.value = false
  }
})

function select(): void {
  if (workflow.value?.latest_version == null) return
  void router.push({
    name: 'requirements',
    params: { id: 'new' },
    query: { workflow_version_id: workflow.value.latest_version.id },
  })
}
</script>

<template>
  <section>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <template v-if="workflow">
      <h1>{{ workflow.name }}</h1>
      <p>{{ workflow.description }}</p>
      <p v-if="workflow.disabled" class="error">该流程已停用，不能再发起新的研究。</p>

      <h2>适用边界</h2>
      <p>{{ workflow.applicability }}</p>

      <h2>必要输入</h2>
      <p>{{ workflow.required_inputs.length > 0 ? workflow.required_inputs.join('、') : '无' }}</p>

      <h2>查询方向</h2>
      <ul>
        <li v-for="direction in workflow.directions" :key="direction.id">
          <strong>{{ direction.title }}</strong>：{{ direction.scope }}
        </li>
      </ul>

      <h2>步骤</h2>
      <ol>
        <li v-for="step in workflow.steps" :key="step.id">
          {{ step.name }} <span class="muted">（{{ step.type }}）</span>
        </li>
      </ol>

      <h2>产出</h2>
      <p>{{ workflow.output.structure ?? '由流程定义' }}</p>

      <p class="muted">
        实际使用版本 v{{ workflow.latest_version?.version_no }} ·
        {{ workflow.latest_version?.definition_hash.slice(0, 20) }}…
      </p>

      <div class="actions">
        <button
          type="button"
          :disabled="workflow.latest_version === null || workflow.disabled"
          @click="select"
        >
          选择该流程
        </button>
        <button
          type="button"
          @click="router.push({ name: 'workflow-versions', params: { id: workflow.id } })"
        >
          版本管理
        </button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.actions {
  display: flex;
  gap: 0.6rem;
  margin-top: 1rem;
}
</style>
