<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import WorkflowDefinitionEditor from '@/components/WorkflowDefinitionEditor.vue'

interface VersionSummary {
  id: string
  version_no: number
  definition_hash: string
  saved_at: string
  change_summary: string
  executable: boolean
  validation_report: { valid: boolean; issues: { code: string; message: string }[] }
  definition: Record<string, unknown>
}

interface WorkflowSummary {
  id: string
  name: string
  revision: number
  disabled: boolean
  latest_version: { id: string; version_no: number } | null
}

const props = defineProps<{ id: string }>()
const client = new ApiClient()

const workflow = ref<WorkflowSummary | null>(null)
const versions = ref<VersionSummary[]>([])
const selected = ref<VersionSummary | null>(null)
const loading = ref(true)
const saving = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)

function describe(caught: unknown): string {
  if (caught instanceof ApiError) {
    if (caught.code === 'VERSION_CHANGED') {
      return `${messageFor(caught.code)}（本地待保存内容已保留）`
    }
    return messageFor(caught.code, caught.message)
  }
  return '操作失败，请稍后重试。'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    workflow.value = await client.get<WorkflowSummary>(`/api/v1/workflows/${props.id}`)
    versions.value = (
      await client.get<{ items: VersionSummary[] }>(`/api/v1/workflows/${props.id}/versions`)
    ).items
    if (selected.value === null && versions.value.length > 0) {
      selected.value = versions.value[0] ?? null
    }
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function selectVersion(version: VersionSummary): void {
  selected.value = version
}

async function save(payload: {
  definition: Record<string, unknown>
  changeSummary: string
  confirmExecutable: boolean
}): Promise<void> {
  if (selected.value === null) return
  saving.value = true
  error.value = null
  notice.value = null
  try {
    const saved = await client.post<VersionSummary & { created: boolean }>(
      `/api/v1/workflows/${props.id}/versions`,
      {
        base_version_id: selected.value.id,
        definition: payload.definition,
        change_summary: payload.changeSummary,
        confirm_executable: payload.confirmExecutable,
      },
    )
    notice.value = saved.created ? `已保存版本 v${saved.version_no}` : '内容未变化，复用已有版本'
    selected.value = null
    await load()
    selected.value = versions.value[0] ?? null
  } catch (caught) {
    // Keep the editor text untouched so local work is never lost on a conflict.
    error.value = describe(caught)
  } finally {
    saving.value = false
  }
}

const disableReason = ref('')
const disableError = ref<string | null>(null)

async function disable(): Promise<void> {
  if (workflow.value === null) return
  if (disableReason.value.trim().length === 0) {
    disableError.value = '请填写停用原因。'
    return
  }
  disableError.value = null
  try {
    await client.post(
      `/api/v1/workflows/${props.id}/disable`,
      { reason: disableReason.value.trim() },
      { etag: `"${workflow.value.revision}"` },
    )
    await load()
    notice.value = '流程已停用；已有运行与历史版本不受影响。'
  } catch (caught) {
    disableError.value = describe(caught)
  }
}
</script>

<template>
  <section>
    <h1>版本管理</h1>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="muted" role="status">{{ notice }}</p>

    <template v-if="workflow">
      <p>
        <strong>{{ workflow.name }}</strong>
        <span v-if="workflow.disabled" class="error">（已停用）</span>
        <span v-if="workflow.latest_version" class="muted">
          · 最新可执行版本 v{{ workflow.latest_version.version_no }}
        </span>
      </p>

      <h2>历史版本</h2>
      <table>
        <thead>
          <tr>
            <th scope="col">版本</th>
            <th scope="col">保存时间</th>
            <th scope="col">变更说明</th>
            <th scope="col">可执行</th>
            <th scope="col">校验</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="version in versions"
            :key="version.id"
            :class="{ selected: version.id === selected?.id }"
          >
            <td>
              <button type="button" @click="selectVersion(version)">v{{ version.version_no }}</button>
            </td>
            <td>{{ version.saved_at }}</td>
            <td>{{ version.change_summary }}</td>
            <td>{{ version.executable ? '是' : '草稿' }}</td>
            <td>{{ version.validation_report.valid ? '通过' : '未通过' }}</td>
          </tr>
        </tbody>
      </table>

      <template v-if="selected">
        <h2>v{{ selected.version_no }} 定义</h2>
        <SafeMarkdown
          v-if="!selected.validation_report.valid"
          :text="
            '**校验未通过**\n\n' +
            selected.validation_report.issues
              .map((issue) => `- ${issue.code}: ${issue.message}`)
              .join('\n')
          "
        />
        <WorkflowDefinitionEditor
          :version-id="selected.id"
          :definition="selected.definition"
          :saving="saving"
          :error-message="error"
          @save="save"
        />
      </template>

      <h2>停用流程</h2>
      <p class="muted">停用只影响新执行；已有运行与历史版本保留。需要当前 revision 的 If-Match。</p>
      <input v-model="disableReason" type="text" maxlength="400" placeholder="停用原因" />
      <p v-if="disableError" class="error" role="alert">{{ disableError }}</p>
      <button type="button" :disabled="workflow.disabled" @click="disable">
        {{ workflow.disabled ? '已停用' : '停用该流程' }}
      </button>
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

tr.selected {
  background: rgb(127 127 127 / 12%);
}

input[type='text'] {
  width: 100%;
  margin-bottom: 0.5rem;
  padding: 0.4rem;
  font: inherit;
}
</style>
