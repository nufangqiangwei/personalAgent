<script setup lang="ts">
/**
 * Branch proposal: pick a start point, review the impact, then confirm explicitly.
 *
 * The start point never moves by itself. Validation recomputes the impact from the
 * real requirement difference; a blocked branch explains why and suggests an earlier
 * point. Creating the branch needs the derived draft's BRANCH confirmation, the final
 * start point and the matching verification report hash.
 */
import { computed, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface Proposal {
  id: string
  parent_run_id: string
  root_run_id: string
  selected_step_id: string
  effective_step_id: string
  draft_id: string
  source_draft_id: string | null
  status: string
  revision: number
  changed_paths: string[]
  input_reads_closure: string[]
  affected_step_ids: string[]
  reusable_step_ids: string[]
  invalidated_artifact_ids: string[]
  blocked_reasons: string[]
  report_hash: string
  confirmation_id: string | null
  created_run_id: string | null
}

interface Analysis {
  status: string
  changed_paths: string[]
  affected_step_ids: string[]
  reusable_step_ids: string[]
  invalidated_artifact_ids: string[]
  blocked_reasons: string[]
  suggested_start_step_id: string | null
  report_hash: string
}

const props = defineProps<{
  parentRunId: string
  steps: { step_id: string; status: string }[]
}>()
const emit = defineEmits<{ created: [runId: string] }>()

const client = new ApiClient()
const selectedStep = ref('')
const proposal = ref<Proposal | null>(null)
const analysis = ref<Analysis | null>(null)
const busy = ref(false)
const error = ref<string | null>(null)
const message = ref<string | null>(null)

const canConfirm = computed(
  () =>
    proposal.value !== null &&
    analysis.value !== null &&
    analysis.value.status === 'READY' &&
    proposal.value.status === 'READY' &&
    !busy.value,
)

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '操作失败。'
}

async function createProposal(): Promise<void> {
  if (!selectedStep.value) {
    error.value = '请选择起点节点。'
    return
  }
  busy.value = true
  error.value = null
  message.value = null
  try {
    proposal.value = await client.post<Proposal>(
      `/api/v1/runs/${props.parentRunId}/branch-proposals`,
      { selected_step_id: selectedStep.value, reason: '从该节点重跑' },
    )
    analysis.value = null
    message.value = '提案已创建。请到要求页修改本次差异，再回来校验。'
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function validate(): Promise<void> {
  if (!proposal.value) return
  busy.value = true
  error.value = null
  try {
    const result = await client.post<{ proposal: Proposal; analysis: Analysis }>(
      `/api/v1/branch-proposals/${proposal.value.id}/validate`,
      { selected_step_id: proposal.value.effective_step_id },
    )
    proposal.value = result.proposal
    analysis.value = result.analysis
    message.value =
      result.analysis.status === 'READY'
        ? '影响范围已确认，可以建立分支。'
        : '当前起点被阻塞，请选择更早的起点。'
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function confirm(): Promise<void> {
  if (!proposal.value || !analysis.value) return
  busy.value = true
  error.value = null
  try {
    const draft = await client.get<{
      revision: number
      summary_hash: string
      workflow_version_id: string
      status: string
      confirmation_id: string | null
    }>(`/api/v1/requirements/${proposal.value.draft_id}`)
    const policy = await client.get<{ version: string; hash: string }>('/api/v1/policies')
    let confirmationId = draft.confirmation_id
    if (!confirmationId) {
      const confirmed = await client.post<{ confirmation_id: string }>(
        `/api/v1/requirements/${proposal.value.draft_id}/confirm`,
        {
          draft_revision: draft.revision,
          summary_hash: draft.summary_hash,
          workflow_version_id: draft.workflow_version_id,
          budget_policy_hash: policy.hash,
          data_policy_version: policy.version,
          action: 'BRANCH',
          explicit_start_confirmation: true,
        },
        { etag: `"${draft.revision}"` },
      )
      confirmationId = confirmed.confirmation_id
    }
    const snapshot = await client.post<{ id: string }>(
      `/api/v1/branch-proposals/${proposal.value.id}/confirm`,
      {
        confirmation_id: confirmationId,
        effective_step_id: proposal.value.effective_step_id,
        report_hash: analysis.value.report_hash,
        explicit_start_confirmation: true,
      },
    )
    message.value = '分支已创建，正在从所选起点继续。'
    emit('created', snapshot.id)
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="branch">
    <h2>从节点重跑（不需要操作 Git）</h2>
    <label>
      起点节点
      <select v-model="selectedStep" :disabled="busy">
        <option value="">请选择</option>
        <option v-for="step in steps" :key="step.step_id" :value="step.step_id">
          {{ step.step_id }}（{{ step.status }}）
        </option>
      </select>
    </label>
    <button type="button" :disabled="busy || !selectedStep" @click="createProposal">
      创建分支提案
    </button>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>

    <template v-if="proposal">
      <dl class="detail">
        <dt>提案状态</dt>
        <dd>{{ proposal.status }}</dd>
        <dt>所选起点</dt>
        <dd>{{ proposal.effective_step_id }}</dd>
        <dt>要求差异</dt>
        <dd>{{ proposal.changed_paths.join('、') || '尚未修改' }}</dd>
      </dl>
      <p>
        <RouterLink :to="`/requirements/${proposal.draft_id}`">
          打开派生要求草稿并修改本次差异
        </RouterLink>
      </p>
      <button type="button" :disabled="busy" @click="validate">校验影响范围</button>

      <template v-if="analysis">
        <h3>核验报告</h3>
        <dl class="detail">
          <dt>结论</dt>
          <dd>{{ analysis.status }}</dd>
          <dt>受影响节点</dt>
          <dd>{{ analysis.affected_step_ids.join('、') || '无' }}</dd>
          <dt>可复用成果</dt>
          <dd>{{ analysis.reusable_step_ids.join('、') || '无' }}</dd>
          <dt>失效成果</dt>
          <dd>{{ analysis.invalidated_artifact_ids.join('、') || '无' }}</dd>
          <dt>阻塞原因</dt>
          <dd>{{ analysis.blocked_reasons.join('；') || '无' }}</dd>
          <dt>建议更早起点</dt>
          <dd>{{ analysis.suggested_start_step_id ?? '无' }}</dd>
        </dl>
        <button type="button" :disabled="!canConfirm" @click="confirm">
          确认新要求与起点，建立分支
        </button>
        <p v-if="!canConfirm" class="muted">
          只有结论为 READY 且影响报告未变化时才能确认建立分支。
        </p>
      </template>
    </template>
  </section>
</template>

<style scoped>
.branch {
  border: 1px solid var(--pa-border);
  padding: 1rem;
  margin-block: 1rem;
}

.detail {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.2rem 0.75rem;
}

dt {
  color: var(--pa-muted);
}

dd {
  margin: 0;
}
</style>
