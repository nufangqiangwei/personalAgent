<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface RunSnapshot {
  id: string
  status: string
  reason_code: string | null
  workflow_version_id: string
  completeness: string | null
  current_step_id: string | null
  budget: {
    limits: Record<string, number>
    consumed: Record<string, number>
    reserved: Record<string, number>
  }
  artifact_ids: string[]
  assistance_ids: string[]
  allowed_actions: { action: string; enabled: boolean; disabled_reason: string | null }[]
  last_event_sequence: number
}

interface StepAttempt {
  attempt_no: number
  status: string
  error_category: string | null
  started_at: string
  ended_at: string | null
  output: Record<string, unknown> | null
}

interface StepView {
  step_id: string
  status: string
  current_attempt_no: number
  skip_reason: string | null
  attempts: StepAttempt[]
}

const props = defineProps<{ id: string }>()
const client = new ApiClient()

const run = ref<RunSnapshot | null>(null)
const steps = ref<StepView[]>([])
const error = ref<string | null>(null)
const loading = ref(true)
let timer: ReturnType<typeof setInterval> | null = null

const terminal = computed(() =>
  ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(run.value?.status ?? ''),
)

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '读取失败。'
}

async function load(): Promise<void> {
  try {
    run.value = await client.get<RunSnapshot>(`/api/v1/runs/${props.id}`)
    steps.value = (
      await client.get<{ items: StepView[] }>(`/api/v1/runs/${props.id}/steps`)
    ).items
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  timer = setInterval(() => {
    if (terminal.value) return
    void load()
  }, 1500)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
})

function directionsOf(attempt: StepAttempt): { id: string; status: string }[] {
  const output = attempt.output
  const directions = output?.directions
  if (!Array.isArray(directions)) return []
  return directions
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => ({ id: String(item.id), status: String(item.status) }))
}
</script>

<template>
  <section>
    <h1>执行进度</h1>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <template v-if="run">
      <dl class="summary">
        <dt>状态</dt>
        <dd>{{ run.status }}<span v-if="run.reason_code" class="muted">（{{ run.reason_code }}）</span></dd>
        <dt>完成程度</dt>
        <dd>{{ run.completeness ?? '尚未产出' }}</dd>
        <dt>当前步骤</dt>
        <dd>{{ run.current_step_id ?? '无' }}</dd>
        <dt>实际版本</dt>
        <dd>{{ run.workflow_version_id }}</dd>
        <dt>额度</dt>
        <dd>
          模型 {{ run.budget.consumed.model_requests ?? 0 }} /
          {{ run.budget.limits.model_requests ?? 0 }} · tokens
          {{ run.budget.consumed.tokens ?? 0 }} / {{ run.budget.limits.tokens ?? 0 }}
        </dd>
      </dl>

      <p v-if="run.status === 'WAITING_USER'" class="warn">
        执行在等待你的明确处置；沉默、查看页面或“知道了”都不会自动继续。
      </p>
      <p v-if="run.assistance_ids.length > 0" class="muted">
        求助记录：{{ run.assistance_ids.join('、') }}
      </p>

      <h2>文档</h2>
      <ul v-if="run.artifact_ids.length > 0">
        <li v-for="artifactId in run.artifact_ids" :key="artifactId">
          <RouterLink :to="`/runs/${run.id}/artifacts/${artifactId}`">查看文档</RouterLink>
        </li>
      </ul>
      <p v-else class="muted">尚未生成可访问的文档。</p>

      <h2>步骤</h2>
      <table>
        <thead>
          <tr>
            <th scope="col">步骤</th>
            <th scope="col">状态</th>
            <th scope="col">尝试</th>
            <th scope="col">方向结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="step in steps" :key="step.step_id">
            <td>{{ step.step_id }}</td>
            <td>
              {{ step.status }}
              <span v-if="step.skip_reason" class="muted">（{{ step.skip_reason }}）</span>
            </td>
            <td>
              <ul>
                <li v-for="attempt in step.attempts" :key="attempt.attempt_no">
                  第 {{ attempt.attempt_no }} 次：{{ attempt.status }}
                  <span v-if="attempt.error_category" class="error">
                    （{{ attempt.error_category }}）
                  </span>
                </li>
              </ul>
            </td>
            <td>
              <ul>
                <li
                  v-for="direction in step.attempts.flatMap(directionsOf)"
                  :key="`${step.step_id}-${direction.id}`"
                >
                  {{ direction.id }}：{{ direction.status }}
                </li>
              </ul>
            </td>
          </tr>
        </tbody>
      </table>

      <h2>可用操作</h2>
      <ul v-if="run.allowed_actions.length > 0">
        <li v-for="action in run.allowed_actions" :key="action.action">
          {{ action.action }}
          <span v-if="!action.enabled" class="muted">
            （{{ action.disabled_reason ?? '不可用' }}）
          </span>
        </li>
      </ul>
      <p v-else class="muted">当前没有可执行的操作；恢复与停止控制在后续阶段提供。</p>
    </template>
  </section>
</template>

<style scoped>
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

.warn {
  color: var(--pa-warn);
}

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
</style>
