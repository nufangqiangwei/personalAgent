<script setup lang="ts">
/**
 * Assistance panel: show what the engine observed and require an explicit decision.
 *
 * Chat discussion is recorded but never unblocks the run. A top-up, an explicit
 * "continue" and the cost acknowledgement are separate fields. A human review needs
 * an answer that satisfies the step's declared decision schema. The server re-reads
 * every unresolved write, so this panel never offers to "declare" it applied.
 */
import { computed, onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'
import type { RunSnapshot, RunStore } from '@/stores/run'

interface Decision {
  decision: string
  continue_authorized: boolean
  approved_increment: Record<string, number> | null
  review_result: Record<string, unknown> | null
  evidence_refs: string[]
  note: string | null
  created_at: string
}

interface Assistance {
  id: string
  run_id: string
  kind: string
  status: string
  revision: number
  limit: Record<string, number>
  consumed: Record<string, number>
  completed_steps: string[]
  remaining_steps: string[]
  observed_problem: string
  unknowns: string[]
  attempted_remedies: string[]
  decision_schema: Record<string, unknown>
  available_decisions: string[]
  created_at: string
  decisions: Decision[]
}

const props = defineProps<{ runId: string; store: RunStore }>()
const client = new ApiClient()

const items = ref<Assistance[]>([])
const error = ref<string | null>(null)
const message = ref<string | null>(null)
const busy = ref(false)
const selected = ref<Record<string, string>>({})
const note = ref<Record<string, string>>({})
const increment = ref<Record<string, string>>({})
const continueAuthorized = ref<Record<string, boolean>>({})
const acknowledgeCost = ref<Record<string, boolean>>({})
const reviewResult = ref<Record<string, string>>({})

const openItems = computed(() => (items.value ?? []).filter((item) => item.status === 'OPEN'))

async function load(): Promise<void> {
  try {
    const response = await client.get<{ items?: Assistance[] }>(
      `/api/v1/runs/${props.runId}/assistance`,
    )
    items.value = response.items ?? []
    for (const item of items.value) {
      selected.value[item.id] ??= item.available_decisions[0] ?? ''
      reviewResult.value[item.id] ??= JSON.stringify(
        defaultReview(item.decision_schema),
        null,
        2,
      )
    }
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  }
}

function defaultReview(schema: Record<string, unknown>): Record<string, unknown> {
  const properties = (schema.properties ?? {}) as Record<string, { enum?: unknown[] }>
  const result: Record<string, unknown> = {}
  for (const [key, rule] of Object.entries(properties)) {
    result[key] = rule.enum?.[0] ?? ''
  }
  return result
}

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '操作失败。'
}

function decisionOf(item: Assistance): string {
  return selected.value[item.id] ?? ''
}

function needsReviewForm(item: Assistance): boolean {
  return item.kind === 'HUMAN_REVIEW' && decisionOf(item) === 'GRANT_AND_CONTINUE'
}

function isBudgetGrant(item: Assistance): boolean {
  return item.kind === 'BUDGET_LIMIT' && decisionOf(item) === 'GRANT_AND_CONTINUE'
}

function parsedIncrement(item: Assistance): Record<string, number> | null {
  const raw = increment.value[item.id] ?? ''
  if (raw.trim() === '') return null
  try {
    const parsed = JSON.parse(raw) as Record<string, number>
    return parsed
  } catch {
    return null
  }
}

async function submit(item: Assistance): Promise<void> {
  busy.value = true
  error.value = null
  message.value = null
  const decision = decisionOf(item)
  const body: Record<string, unknown> = {
    revision: item.revision,
    decision,
    note: note.value[item.id] ?? null,
  }
  if (isBudgetGrant(item)) {
    const parsed = parsedIncrement(item)
    if (parsed === null) {
      error.value = '追加额度需要合法的 JSON，例如 {"tokens": 20000}。'
      busy.value = false
      return
    }
    body.approved_increment = parsed
    body.acknowledge_cost = acknowledgeCost.value[item.id] === true
    body.continue_authorized = continueAuthorized.value[item.id] === true
  }
  if (needsReviewForm(item)) {
    try {
      body.review_result = JSON.parse(reviewResult.value[item.id] ?? '{}')
    } catch {
      error.value = '审核结果必须是合法 JSON。'
      busy.value = false
      return
    }
    body.continue_authorized = continueAuthorized.value[item.id] === true
  }
  try {
    const result = await client.post<{ run?: RunSnapshot }>(
      `/api/v1/runs/${props.runId}/assistance/${item.id}/decisions`,
      body,
      { etag: `"${item.revision}"` },
    )
    if (result.run) props.store.applySnapshot(result.run)
    message.value = '处置已记录。'
    await load()
    await props.store.load()
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="assistance">
    <h2>求助与处置</h2>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="openItems.length === 0" class="muted">当前没有待处理的求助。</p>

    <article v-for="item in items" :key="item.id" class="request">
      <header>
        <strong>{{ item.kind }}</strong>
        <span class="muted">（{{ item.status }}）</span>
      </header>
      <p>{{ item.observed_problem }}</p>
      <dl class="detail">
        <template v-if="Object.keys(item.limit).length > 0">
          <dt>上限</dt>
          <dd>{{ JSON.stringify(item.limit) }}</dd>
          <dt>已消耗</dt>
          <dd>{{ JSON.stringify(item.consumed) }}</dd>
        </template>
        <dt>已完成步骤</dt>
        <dd>{{ item.completed_steps.join('、') || '无' }}</dd>
        <dt>剩余步骤</dt>
        <dd>{{ item.remaining_steps.join('、') || '无' }}</dd>
        <dt>未知</dt>
        <dd>{{ item.unknowns.join('、') || '无' }}</dd>
        <dt>已尝试</dt>
        <dd>{{ item.attempted_remedies.join('、') || '无' }}</dd>
        <dt>可选处置</dt>
        <dd>{{ item.available_decisions.join(' / ') }}</dd>
      </dl>

      <ul v-if="item.decisions.length > 0" class="muted">
        <li v-for="decision in item.decisions" :key="decision.created_at">
          {{ decision.decision }}<span v-if="decision.note">：{{ decision.note }}</span>
        </li>
      </ul>

      <template v-if="item.status === 'OPEN'">
        <label>
          处置
          <select v-model="selected[item.id]">
            <option v-for="choice in item.available_decisions" :key="choice" :value="choice">
              {{ choice }}
            </option>
          </select>
        </label>
        <label>
          说明
          <input v-model="note[item.id]" type="text" maxlength="2000" />
        </label>

        <template v-if="isBudgetGrant(item)">
          <label>
            追加额度（JSON，例如 {"tokens": 20000}）
            <input v-model="increment[item.id]" type="text" />
          </label>
          <label class="inline">
            <input v-model="continueAuthorized[item.id]" type="checkbox" />
            明确授权继续执行
          </label>
          <label class="inline">
            <input v-model="acknowledgeCost[item.id]" type="checkbox" />
            我已知晓费用与账本
          </label>
        </template>

        <template v-if="needsReviewForm(item)">
          <label>
            审核结果（须符合该步骤声明的结果契约）
            <textarea v-model="reviewResult[item.id]" rows="4" />
          </label>
          <label class="inline">
            <input v-model="continueAuthorized[item.id]" type="checkbox" />
            我已作答并授权继续
          </label>
        </template>

        <button type="button" :disabled="busy || !decisionOf(item)" @click="submit(item)">
          提交处置
        </button>
        <p class="muted">仅讨论或沉默不会解除阻塞；写入核验由服务端重新读取目标。</p>
      </template>
    </article>
  </section>
</template>

<style scoped>
.assistance {
  border: 1px solid var(--pa-border);
  padding: 1rem;
  margin-block: 1rem;
}

.request {
  border-top: 1px solid var(--pa-border);
  padding-block: 0.75rem;
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

.inline {
  display: inline-flex;
  gap: 0.25rem;
  align-items: center;
}

label {
  display: block;
  margin-block: 0.4rem;
}
</style>
