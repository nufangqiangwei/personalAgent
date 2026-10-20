<script setup lang="ts">
/**
 * Saved data: purpose, references, expiry and deletion impact.
 *
 * Deleting is explicit and immediate for reads and reuse; the physical cleanup runs
 * afterwards and can be polled through the deletion status URL. A shared blob is kept
 * while another available document still needs it.
 */
import { computed, onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface DataItem {
  id: string
  type: string
  run_id: string
  structure: string
  completeness: string
  access_state: string
  created_at: string
  expires_at: string | null
  shared_artifact_references: number
  deletable: boolean
  keeps_shared_blob: boolean
}

interface DeletionRecord {
  id: string
  status: string
  affected_ids: string[]
  requested_at: string
  completed_at: string | null
  status_url: string
}

const client = new ApiClient()
const items = ref<DataItem[]>([])
const selected = ref<string[]>([])
const acknowledge = ref(false)
const error = ref<string | null>(null)
const message = ref<string | null>(null)
const busy = ref(false)
const deletion = ref<DeletionRecord | null>(null)

const selectedItems = computed(() =>
  items.value.filter((item) => selected.value.includes(item.id)),
)
const sharedWarning = computed(() =>
  selectedItems.value.some((item) => item.keeps_shared_blob),
)

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '操作失败。'
}

async function load(): Promise<void> {
  try {
    items.value = (await client.get<{ items: DataItem[] }>('/api/v1/data-items')).items
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  }
}

function toggle(id: string): void {
  selected.value = selected.value.includes(id)
    ? selected.value.filter((item) => item !== id)
    : [...selected.value, id]
}

async function remove(): Promise<void> {
  if (selected.value.length === 0) {
    error.value = '请先选择要删除的内容。'
    return
  }
  if (!acknowledge.value) {
    error.value = '删除需要勾选影响确认。'
    return
  }
  busy.value = true
  error.value = null
  try {
    deletion.value = await client.post<DeletionRecord>('/api/v1/deletions', {
      item_ids: selected.value,
      acknowledge_resume_impact: true,
    })
    selected.value = []
    message.value = '删除已生效；物理清理在后台进行，可轮询状态。'
    await load()
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function poll(): Promise<void> {
  if (!deletion.value) return
  try {
    deletion.value = await client.get<DeletionRecord>(deletion.value.status_url)
    await load()
  } catch (caught) {
    error.value = describe(caught)
  }
}

onMounted(load)
</script>

<template>
  <section>
    <h1>数据与删除</h1>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>

    <table v-if="items.length > 0">
      <thead>
        <tr>
          <th scope="col">选择</th>
          <th scope="col">类型</th>
          <th scope="col">来源执行</th>
          <th scope="col">状态</th>
          <th scope="col">创建时间</th>
          <th scope="col">到期</th>
          <th scope="col">共享引用</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td>
            <input
              type="checkbox"
              :checked="selected.includes(item.id)"
              :disabled="!item.deletable"
              @change="toggle(item.id)"
            />
          </td>
          <td>{{ item.type }} / {{ item.structure }}</td>
          <td>{{ item.run_id.slice(0, 8) }}</td>
          <td>{{ item.access_state }}</td>
          <td>{{ item.created_at }}</td>
          <td>{{ item.expires_at ?? '按保留政策' }}</td>
          <td>{{ item.shared_artifact_references }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">还没有保存的文档。</p>

    <h2>删除影响</h2>
    <p class="muted">
      删除后普通查询不再返回内容，分支也不能复用；相关备份中的删除墓碑会在恢复时重新应用。
    </p>
    <p v-if="sharedWarning" class="warn">
      所选内容与其他文档共享同一文件；仅当没有其他可用引用时才会清理物理文件。
    </p>
    <label class="inline">
      <input v-model="acknowledge" type="checkbox" />
      我确认删除会影响后续继续与分支复用
    </label>
    <button type="button" :disabled="busy || selected.length === 0" @click="remove">
      删除所选（{{ selected.length }}）
    </button>

    <template v-if="deletion">
      <h2>删除记录</h2>
      <dl class="summary">
        <dt>状态</dt>
        <dd>{{ deletion.status }}</dd>
        <dt>请求时间</dt>
        <dd>{{ deletion.requested_at }}</dd>
        <dt>完成时间</dt>
        <dd>{{ deletion.completed_at ?? '清理中' }}</dd>
      </dl>
      <button type="button" @click="poll">刷新清理状态</button>
    </template>
  </section>
</template>

<style scoped>
table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  padding: 0.4rem;
  border-bottom: 1px solid var(--pa-border);
  text-align: left;
  vertical-align: top;
}

.summary {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.25rem 1rem;
}

dt {
  color: var(--pa-muted);
}

dd {
  margin: 0;
}

.inline {
  display: inline-flex;
  gap: 0.25rem;
  align-items: center;
  margin-inline-end: 1rem;
}

.warn {
  color: var(--pa-warn);
}
</style>
