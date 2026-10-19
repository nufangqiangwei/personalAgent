<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface WorkflowItem {
  id: string
  name: string
  description: string
  applicability: string
  disabled: boolean
  latest_version: { id: string; version_no: number } | null
  directions: { id: string; title: string }[]
  required_inputs: string[]
  steps: { id: string; name: string; type: string }[]
}

const client = new ApiClient()
const router = useRouter()

const items = ref<WorkflowItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const response = await client.get<{ items: WorkflowItem[] }>('/api/v1/workflows')
    items.value = response.items
  } catch (caught) {
    error.value = caught instanceof ApiError ? messageFor(caught.code, caught.message) : '加载失败。'
  } finally {
    loading.value = false
  }
}

onMounted(load)

function select(item: WorkflowItem): void {
  if (item.latest_version === null) return
  void router.push({
    name: 'requirements',
    params: { id: 'new' },
    query: { workflow_version_id: item.latest_version.id },
  })
}

function openDetail(item: WorkflowItem): void {
  void router.push({ name: 'workflow-detail', params: { id: item.id } })
}
</script>

<template>
  <section>
    <h1>固定流程</h1>
    <p class="muted">先选择流程，再在聊天中补齐要求。列表不需要先描述需求。</p>

    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <div v-if="!loading && items.length === 0" class="empty">
      <p>当前没有可用的固定流程。</p>
      <p class="muted">你可以返回，或结束本次操作；系统不会自动匹配或进入探索。</p>
    </div>

    <ul class="cards">
      <li v-for="item in items" :key="item.id" class="card">
        <div>
          <h2>{{ item.name }}</h2>
          <p>{{ item.description }}</p>
          <dl>
            <dt>适用边界</dt>
            <dd>{{ item.applicability }}</dd>
            <dt>查询方向</dt>
            <dd>{{ item.directions.map((direction) => direction.title).join('、') }}</dd>
            <dt>必要输入</dt>
            <dd>{{ item.required_inputs.length > 0 ? item.required_inputs.join('、') : '无' }}</dd>
            <dt>最新版本</dt>
            <dd>v{{ item.latest_version?.version_no ?? '-' }}</dd>
          </dl>
        </div>
        <div class="actions">
          <button
            type="button"
            :disabled="item.latest_version === null"
            @click="select(item)"
          >
            选择该流程
          </button>
          <button type="button" @click="openDetail(item)">查看说明</button>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.cards {
  display: grid;
  gap: 1rem;
  padding: 0;
  list-style: none;
}

.card {
  display: flex;
  gap: 1rem;
  justify-content: space-between;
  padding: 1rem;
  border: 1px solid var(--pa-border);
  border-radius: 0.5rem;
}

dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.2rem 0.8rem;
  margin: 0;
}

dt {
  color: var(--pa-muted);
}

dd {
  margin: 0;
}

.actions {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-width: 8rem;
}

.empty {
  padding: 1rem;
  border: 1px dashed var(--pa-border);
  border-radius: 0.5rem;
}
</style>
