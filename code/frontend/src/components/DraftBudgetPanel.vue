<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface BudgetSnapshot {
  limits: Record<string, number>
  consumed: Record<string, number>
  reserved: Record<string, number>
  remaining: Record<string, number>
  exhausted: boolean
  exhausted_dimensions: string[]
  assistance?: {
    kind: string
    status: string
    observed_problem: string
    unknown_model_request: boolean
    available_actions: { action: string; enabled: boolean; disabled_reason: string | null }[]
  } | null
}

const props = defineProps<{
  draftId: string
  draftRevision: number
  draftStatus: string
}>()

const emit = defineEmits<{ granted: [] }>()

const client = new ApiClient()
const budget = ref<BudgetSnapshot | null>(null)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const extraModels = ref(5)
const extraTokens = ref(5000)

const grantable = computed(
  () => props.draftStatus === 'COLLECTING' || props.draftStatus === 'READY',
)
const canGrant = computed(
  () => grantable.value && budget.value?.exhausted === true && !busy.value,
)

async function load(): Promise<void> {
  try {
    budget.value = await client.get<BudgetSnapshot>(
      `/api/v1/requirements/${props.draftId}/budget`,
    )
  } catch (caught) {
    error.value = caught instanceof ApiError ? messageFor(caught.code, caught.message) : '额度读取失败。'
  }
}

onMounted(load)
watch(() => props.draftRevision, load)

async function grant(): Promise<void> {
  if (budget.value === null) return
  busy.value = true
  error.value = null
  notice.value = null
  const increment: Record<string, number> = {}
  if (extraModels.value > 0) increment.model_requests = extraModels.value
  if (extraTokens.value > 0) increment.tokens = extraTokens.value
  try {
    await client.post(
      `/api/v1/requirements/${props.draftId}/budget-grants`,
      {
        draft_revision: props.draftRevision,
        approved_increment: increment,
        explicit_grant: true,
        acknowledge_cost_and_ledger: true,
      },
      { etag: `"${props.draftRevision}"` },
    )
    notice.value = '已追加草稿额度；研究不会自动开始，仍需你显式重试分析。'
    await load()
    emit('granted')
  } catch (caught) {
    error.value = caught instanceof ApiError ? messageFor(caught.code, caught.message) : '追加失败。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="budget" aria-label="草稿额度">
    <h3>草稿额度（独立账本）</h3>
    <p class="muted">
      草稿消耗与后续研究分别记账；追加只解除额度阻塞，不会创建执行或确认。
    </p>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>

    <template v-if="budget">
      <dl>
        <dt>模型请求</dt>
        <dd>
          {{ budget.consumed.model_requests ?? 0 }} / {{ budget.limits.model_requests ?? 0 }}
          （剩余 {{ budget.remaining.model_requests ?? 0 }}）
        </dd>
        <dt>Tokens</dt>
        <dd>
          {{ budget.consumed.tokens ?? 0 }} / {{ budget.limits.tokens ?? 0 }}
          （剩余 {{ budget.remaining.tokens ?? 0 }}）
        </dd>
      </dl>

      <p v-if="budget.exhausted" class="error">
        已触顶维度：{{ budget.exhausted_dimensions.join('、') }}
      </p>
      <p v-if="budget.assistance" class="muted">{{ budget.assistance.observed_problem }}</p>

      <div v-if="budget.exhausted" class="grant">
        <label>
          追加模型请求
          <input v-model.number="extraModels" type="number" min="0" max="200" />
        </label>
        <label>
          追加 tokens
          <input v-model.number="extraTokens" type="number" min="0" max="300000" step="1000" />
        </label>
        <button type="button" :disabled="!canGrant" @click="grant">
          {{ busy ? '追加中…' : '明确追加额度' }}
        </button>
        <p v-if="!grantable" class="muted">
          当前草稿状态（{{ draftStatus }}）不允许追加。
        </p>
      </div>
    </template>
  </section>
</template>

<style scoped>
dl {
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

.grant {
  display: flex;
  gap: 0.75rem;
  align-items: flex-end;
  flex-wrap: wrap;
}

.grant label {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.grant input {
  width: 7rem;
  padding: 0.25rem;
  font: inherit;
}
</style>
