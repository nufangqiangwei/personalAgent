<script setup lang="ts">
/**
 * Run progress: server snapshot plus committed events, with stop/resume controls and
 * the assistance panel. Buttons come from `allowed_actions`; a draining run shows the
 * actions still in flight. The page never advances state itself.
 */
import { computed, onBeforeUnmount, onMounted } from 'vue'

import AssistancePanel from '@/components/AssistancePanel.vue'
import RunControls from '@/components/RunControls.vue'
import { createRunStore } from '@/stores/run'
import type { StepAttempt, StepView } from '@/stores/run'

const props = defineProps<{ id: string }>()

const store = createRunStore(props.id)
const lateCount = computed(() => store.state.lateEvents.length)
let timer: ReturnType<typeof setInterval> | null = null

function directionsOf(attempt: StepAttempt): { id: string; status: string }[] {
  const output = attempt.output
  const directions = output?.directions
  if (!Array.isArray(directions)) return []
  return directions
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => ({ id: String(item.id), status: String(item.status) }))
}

function stepsOf(): StepView[] {
  return store.state.steps
}

onMounted(async () => {
  await store.load()
  if (typeof EventSource !== 'undefined') {
    store.connect()
  }
  // Page refresh fallback: a slow or blocked stream still converges through the
  // committed history rather than a second polling timer.
  timer = setInterval(() => {
    if (store.terminal.value) return
    void store.backfill().catch(() => undefined)
    void store.load()
  }, 3000)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
  store.disconnect()
})
</script>

<template>
  <section>
    <h1>执行进度</h1>
    <p v-if="store.state.loading" class="muted">加载中…</p>
    <p v-if="store.state.error" class="error" role="alert">{{ store.state.error }}</p>
    <p v-if="store.state.resetRequired" class="warn" role="status">
      事件游标已被清理，已重新读取执行快照；缺失区间以快照为准。
    </p>

    <template v-if="store.state.run">
      <dl class="summary">
        <dt>状态</dt>
        <dd>
          {{ store.state.run.status }}
          <span v-if="store.state.run.reason_code" class="muted">
            （{{ store.state.run.reason_code }}）
          </span>
          <span v-if="store.draining.value" class="warn">（正在收敛）</span>
        </dd>
        <dt>完成程度</dt>
        <dd>{{ store.state.run.completeness ?? '尚未产出' }}</dd>
        <dt>当前步骤</dt>
        <dd>{{ store.state.run.current_step_id ?? '无' }}</dd>
        <dt>实际版本</dt>
        <dd>{{ store.state.run.workflow_version_id }}</dd>
        <dt>额度</dt>
        <dd>
          模型 {{ store.state.run.budget.consumed.model_requests ?? 0 }} /
          {{ store.state.run.budget.limits.model_requests ?? 0 }} · tokens
          {{ store.state.run.budget.consumed.tokens ?? 0 }} /
          {{ store.state.run.budget.limits.tokens ?? 0 }}
        </dd>
      </dl>

      <p v-if="store.state.run.status === 'WAITING_USER'" class="warn">
        执行在等待你的明确处置；沉默、查看页面或“知道了”都不会自动继续。
      </p>
      <ul v-if="store.inFlight.value.length > 0" class="muted">
        <li v-for="action in store.inFlight.value" :key="action.id">
          在途：{{ action.type }}（{{ action.status }}）
        </li>
      </ul>

      <RunControls :run-id="store.state.run.id" :store="store" />
      <AssistancePanel :run-id="store.state.run.id" :store="store" />

      <h2>文档</h2>
      <ul v-if="store.state.run.artifact_ids.length > 0">
        <li v-for="artifactId in store.state.run.artifact_ids" :key="artifactId">
          <RouterLink :to="`/runs/${store.state.run.id}/artifacts/${artifactId}`">
            查看文档
          </RouterLink>
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
          <tr v-for="step in stepsOf()" :key="step.step_id">
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

      <h2>事件</h2>
      <p v-if="store.state.events.length === 0" class="muted">尚未收到已提交事件。</p>
      <ul v-else>
        <li v-for="event in store.state.events.slice(-20)" :key="event.data.sequence">
          #{{ event.data.sequence }} {{ event.event }}
          <span v-if="event.late" class="warn">（迟到事件，仅作为证据）</span>
        </li>
      </ul>
      <p v-if="lateCount > 0" class="muted">
        共 {{ lateCount }} 条迟到事件；它们不会把已结束的执行改成成功。
      </p>

      <p>
        <RouterLink :to="`/runs`">查看执行历史与分支</RouterLink>
      </p>
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
