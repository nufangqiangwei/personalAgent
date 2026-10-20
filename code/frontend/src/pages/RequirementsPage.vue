<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiClient, ApiError, messageFor } from '@/api/client'
import DraftBudgetPanel from '@/components/DraftBudgetPanel.vue'
import RequirementSummary from '@/components/RequirementSummary.vue'

interface FieldEntry {
  path: string
  value: unknown
  source: string
  accepted?: boolean
}

interface SuggestionEntry {
  id: string
  field_path: string
  value: unknown
  rationale: string
  status: string
}

interface Draft {
  id: string
  revision: number
  workflow_version_id: string
  workflow_name: string
  workflow_version_no: number
  status: string
  fields: FieldEntry[]
  missing_fields: string[]
  conflicts: { path?: string; message?: string }[]
  preference_order: string[]
  output_spec: Record<string, unknown>
  summary_markdown: string
  summary_hash: string
  suggestions: SuggestionEntry[]
  messages: { id: string; sequence: number; role: string; content: string }[]
  confirmation_id: string | null
}

interface Policy {
  version: string
  hash: string
}

const props = defineProps<{ id: string }>()
const route = useRoute()
const router = useRouter()
const client = new ApiClient()
const branchProposalId = computed(() => typeof route.query.branch_proposal_id === 'string'
  ? route.query.branch_proposal_id : null)

const draft = ref<Draft | null>(null)
const policy = ref<Policy | null>(null)
const text = ref('')
const loading = ref(true)
const busy = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const explicitConfirm = ref(false)
const analysisStatus = ref<string | null>(null)

const confirmable = computed(() => {
  const current = draft.value
  if (current === null) return false
  if (current.status === 'CONFIRMED') return false
  if (current.missing_fields.length > 0 || current.conflicts.length > 0) return false
  if (current.suggestions.some((item) => item.status === 'PENDING')) return false
  return explicitConfirm.value
})

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '操作失败，请重试。'
}

async function createDraft(): Promise<void> {
  const versionId = String(route.query.workflow_version_id ?? '')
  if (!versionId) {
    error.value = '缺少 workflow_version_id；请从固定流程列表选择。'
    return
  }
  const created = await client.post<Draft>('/api/v1/requirements', {
    workflow_version_id: versionId,
  })
  draft.value = created
  await router.replace({ name: 'requirements', params: { id: created.id } })
}

async function load(): Promise<void> {
  loading.value = true
  try {
    policy.value = await client.get<Policy>('/policies')
    if (props.id === 'new') {
      await createDraft()
    } else {
      draft.value = await client.get<Draft>(`/api/v1/requirements/${props.id}`)
    }
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function send(): Promise<void> {
  if (draft.value === null || text.value.trim().length === 0) return
  busy.value = true
  error.value = null
  notice.value = null
  analysisStatus.value = null
  try {
    const result = await client.post<{
      message_id: string
      analysis_operation_id: string
      status: string
    }>(`/api/v1/requirements/${draft.value.id}/messages`, { text: text.value.trim() })
    analysisStatus.value = result.status
    if (result.status === 'WAITING_MODEL') {
      notice.value = '模型正忙，消息已保留为待分析；稍后再次发送或等待后再试。'
    } else if (result.status === 'UNKNOWN') {
      notice.value = '上一次模型结果无法确认，解决前不会继续调用模型。'
    } else if (result.status === 'FAILED') {
      notice.value = '分析未完成；请查看额度或稍后重试。'
    }
    text.value = ''
    draft.value = await client.get<Draft>(`/api/v1/requirements/${draft.value.id}`)
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function accept(suggestion: SuggestionEntry): Promise<void> {
  if (draft.value === null) return
  busy.value = true
  error.value = null
  try {
    draft.value = await client.post<Draft>(
      `/api/v1/requirements/${draft.value.id}/suggestions/${suggestion.id}/accept`,
      { draft_revision: draft.value.revision },
      { etag: `"${draft.value.revision}"` },
    )
    notice.value = '建议已接受；仍需确认完整要求后才会执行。'
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function reject(suggestion: SuggestionEntry): Promise<void> {
  if (draft.value === null) return
  busy.value = true
  error.value = null
  try {
    draft.value = await client.post<Draft>(
      `/api/v1/requirements/${draft.value.id}/suggestions/${suggestion.id}/reject`,
      { draft_revision: draft.value.revision },
      { etag: `"${draft.value.revision}"` },
    )
    notice.value = '建议已拒绝；不会进入本次要求。'
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function confirm(): Promise<void> {
  const current = draft.value
  const policies = policy.value
  if (current === null || policies === null) return
  busy.value = true
  error.value = null
  try {
    const result = await client.post<{ confirmation_id: string }>(
      `/api/v1/requirements/${current.id}/confirm`,
      {
        draft_revision: current.revision,
        summary_hash: current.summary_hash,
        workflow_version_id: current.workflow_version_id,
        budget_policy_hash: policies.hash,
        data_policy_version: policies.version,
        action: branchProposalId.value ? 'BRANCH' : 'START',
        explicit_start_confirmation: true,
      },
      { etag: `"${current.revision}"` },
    )
    notice.value = branchProposalId.value
      ? `已确认（${result.confirmation_id}）。返回分支提案确认起点后才会创建分支。`
      : `已确认（${result.confirmation_id}）。点击“开始研究”才会创建执行。`
    draft.value = await client.get<Draft>(`/api/v1/requirements/${current.id}`)
    explicitConfirm.value = false
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function startResearch(): Promise<void> {
  const confirmationId = draft.value?.confirmation_id
  if (!confirmationId) return
  busy.value = true
  error.value = null
  try {
    const snapshot = await client.post<{ id: string }>('/api/v1/runs', {
      confirmation_id: confirmationId,
    })
    notice.value = '研究已启动。'
    await router.push(`/runs/${snapshot.id}`)
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section>
    <h1>研究要求</h1>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>

    <template v-if="draft">
      <p>
        <strong>{{ draft.workflow_name }}</strong>
        <span class="muted"> · 版本 v{{ draft.workflow_version_no }} · 状态 {{ draft.status }}</span>
      </p>

      <section aria-label="聊天记录">
        <h2>聊天</h2>
        <p class="muted">要求只能通过聊天修改；不接受直接编辑要求列表。</p>
        <ol class="messages">
          <li v-for="message in draft.messages" :key="message.id" :class="message.role">
            <span class="role">{{ message.role === 'user' ? '你' : '助手' }}</span>
            <span>{{ message.content }}</span>
          </li>
        </ol>
        <label for="message-text">补充要求</label>
        <textarea
          id="message-text"
          v-model="text"
          rows="3"
          placeholder="例如：12 人团队，预算每人每月 40 元，需要私有化部署"
          :disabled="busy || draft.status === 'CONFIRMED'"
        />
        <button type="button" :disabled="busy || text.trim().length === 0" @click="send">
          {{ busy ? '处理中…' : '发送并分析' }}
        </button>
        <p v-if="analysisStatus" class="muted">分析状态：{{ analysisStatus }}</p>
      </section>

      <section v-if="draft.suggestions.length > 0" aria-label="建议">
        <h2>待确认建议</h2>
        <ul>
          <li v-for="suggestion in draft.suggestions" :key="suggestion.id">
            <strong>{{ suggestion.field_path }}</strong>：{{ suggestion.value ?? '（空）' }}
            <span class="muted">依据：{{ suggestion.rationale }}</span>
            <span v-if="suggestion.status === 'PENDING'">
              <button type="button" :disabled="busy" @click="accept(suggestion)">接受建议</button>
              <button type="button" :disabled="busy" @click="reject(suggestion)">拒绝建议</button>
            </span>
            <span v-else class="muted">（{{ suggestion.status }}）</span>
          </li>
        </ul>
      </section>

      <h2>完整摘要</h2>
      <RequirementSummary
        :workflow-name="draft.workflow_name"
        :workflow-version-no="draft.workflow_version_no"
        :summary-markdown="draft.summary_markdown"
        :fields="draft.fields"
        :missing-fields="draft.missing_fields"
        :conflicts="draft.conflicts"
      />

      <DraftBudgetPanel
        :draft-id="draft.id"
        :draft-revision="draft.revision"
        :draft-status="draft.status"
      />

      <section aria-label="最终确认">
        <h2>最终确认</h2>
        <p v-if="draft.confirmation_id" class="muted">
          已确认：{{ draft.confirmation_id }}
        </p>
        <label class="checkbox">
          <input v-model="explicitConfirm" type="checkbox" :disabled="busy" />
          我已核对完整摘要，确认按以上要求执行
        </label>
        <button type="button" :disabled="!confirmable || busy" @click="confirm">
          {{ busy ? '确认中…' : '确认要求' }}
        </button>
        <p v-if="draft.status === 'CONFIRMED'" class="muted">该草稿已确认，修改要求需回到聊天。</p>
        <button
          v-if="!branchProposalId && draft.status === 'CONFIRMED' && draft.confirmation_id"
          type="button"
          :disabled="busy"
          @click="startResearch"
        >
          {{ busy ? '启动中…' : '开始研究' }}
        </button>
        <RouterLink v-if="branchProposalId" :to="{ path: '/runs', query: {
          run: route.query.parent_run_id, proposal: branchProposalId,
        } }">返回分支提案并确认起点</RouterLink>
        <p v-if="!branchProposalId && draft.status === 'CONFIRMED' && draft.confirmation_id" class="muted">
          确认本身不会启动研究；点击“开始研究”才会创建执行。
        </p>
      </section>
    </template>
  </section>
</template>

<style scoped>
.messages {
  padding-left: 1.2rem;
}

.messages .role {
  margin-right: 0.5rem;
  color: var(--pa-muted);
}

.messages li.assistant {
  color: var(--pa-muted);
}

textarea {
  width: 100%;
  font: inherit;
  margin-bottom: 0.4rem;
}

.checkbox {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin: 0.5rem 0;
}
</style>
