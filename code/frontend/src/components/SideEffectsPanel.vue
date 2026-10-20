<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface WriteOperation { id: string; status: string; target_ref: string }
const props = defineProps<{ runId: string }>()
const client = new ApiClient()
const items = ref<WriteOperation[]>([])
const error = ref<string | null>(null)
const busy = ref(false)
const message = ref<string | null>(null)
const unresolved = new Set(['INTENT', 'UNKNOWN', 'PARTIAL', 'CONFLICT'])

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '核验失败，请重试。'
}

async function load(): Promise<void> {
  try {
    items.value = (await client.get<{ items: WriteOperation[] }>(
      `/api/v1/runs/${props.runId}/side-effects`,
    )).items
  } catch (caught) { error.value = describe(caught) }
}

async function reconcile(item: WriteOperation): Promise<void> {
  busy.value = true
  error.value = null
  try {
    const result = await client.post<{ resolved: boolean }>(
      `/api/v1/runs/${props.runId}/side-effects/${item.id}/reconcile`,
      { evidence_refs: [], decision_note: '用户请求服务端核验实际文件' },
    )
    message.value = result.resolved
      ? '已读取实际文件并核验写入结果。继续或建立分支仍需明确操作。'
      : '仍无法确认写入结果，相关执行继续保持阻塞。'
    await load()
  } catch (caught) { error.value = describe(caught) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section aria-label="文件写入核验">
    <h2>文件写入核验</h2>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="items.length === 0">暂无文件写入记录。</p>
    <ul v-else>
      <li v-for="item in items" :key="item.id">
        {{ item.target_ref }}：<span>{{ item.status }}</span>
        <button v-if="unresolved.has(item.status)" :disabled="busy" @click="reconcile(item)">
          核验实际写入
        </button>
      </li>
    </ul>
    <p>核验由应用读取实际文件完成；口头声明不会解除写入阻塞。</p>
  </section>
</template>
