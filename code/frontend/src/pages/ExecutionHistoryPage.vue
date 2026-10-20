<script setup lang="ts">
/**
 * Execution history: root, parent/child branches, fork points, source artifacts.
 *
 * The page reads trusted records only. Re-running from a node creates a branch
 * proposal; it never asks the user to know anything about Git.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiClient, ApiError, messageFor } from '@/api/client'
import BranchProposalPanel from '@/components/BranchProposalPanel.vue'
import SideEffectsPanel from '@/components/SideEffectsPanel.vue'

interface RunSummary {
  id: string
  parent_run_id: string | null
  fork_step_id: string | null
  status: string
  revision: number
  workflow_version_id: string
  completeness: string | null
  artifact_ids: string[]
  created_at: string
}

interface HistoryEntry extends RunSummary {
  root_run_id?: string
  steps: { step_id: string; status: string; selected_transition: string | null }[]
}

interface StepView {
  step_id: string
  status: string
  selected_transition: string | null
  skip_reason: string | null
}

const client = new ApiClient()
const route = useRoute()
const router = useRouter()
const runs = ref<RunSummary[]>([])
const selectedId = ref<string | null>(null)
const history = ref<{ root_run_id: string; items: HistoryEntry[] } | null>(null)
const steps = ref<StepView[]>([])
const error = ref<string | null>(null)
const loading = ref(true)
const nextCursor = ref<string | null>(null)

const selected = computed(() => runs.value.find((run) => run.id === selectedId.value) ?? null)

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '读取失败。'
}

async function loadRuns(): Promise<void> {
  try {
    const page = await client.get<{ items: RunSummary[]; next_cursor: string | null }>(
      '/api/v1/runs',
    )
    runs.value = page.items
    nextCursor.value = page.next_cursor
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    loading.value = false
  }
}

async function loadMore(): Promise<void> {
  if (nextCursor.value === null) return
  try {
    const page = await client.get<{ items: RunSummary[]; next_cursor: string | null }>(
      `/api/v1/runs?cursor=${encodeURIComponent(nextCursor.value)}`,
    )
    runs.value = [...runs.value, ...page.items]
    nextCursor.value = page.next_cursor
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  }
}

async function loadDetail(runId: string): Promise<void> {
  try {
    history.value = await client.get<{ root_run_id: string; items: HistoryEntry[] }>(
      `/api/v1/runs/${runId}/history`,
    )
    steps.value = (
      await client.get<{ items: StepView[] }>(`/api/v1/runs/${runId}/steps`)
    ).items
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  }
}

function select(runId: string): void {
  selectedId.value = runId
  void router.replace({ path: '/runs', query: { run: runId } })
}

function onBranchCreated(runId: string): void {
  selectedId.value = runId
  void loadRuns()
}

watch(selectedId, (value) => {
  if (value) void loadDetail(value)
})

onMounted(async () => {
  await loadRuns()
  if (typeof route.query.run === 'string') selectedId.value = route.query.run
})
</script>

<template>
  <section>
    <h1>执行历史与分支</h1>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <table v-if="runs.length > 0">
      <thead>
        <tr>
          <th scope="col">执行</th>
          <th scope="col">状态</th>
          <th scope="col">父执行</th>
          <th scope="col">分叉节点</th>
          <th scope="col">完成程度</th>
          <th scope="col">文档</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="run.id">
          <td>
            <button type="button" class="link" @click="select(run.id)">
              {{ run.id.slice(0, 8) }}
            </button>
            <RouterLink :to="`/runs/${run.id}`">进度</RouterLink>
          </td>
          <td>{{ run.status }}</td>
          <td>{{ run.parent_run_id ? run.parent_run_id.slice(0, 8) : '根执行' }}</td>
          <td>{{ run.fork_step_id ?? '无' }}</td>
          <td>{{ run.completeness ?? '尚未产出' }}</td>
          <td>{{ run.artifact_ids.length }} 份</td>
        </tr>
      </tbody>
    </table>
    <button v-if="nextCursor" type="button" :disabled="loading" @click="loadMore">
      加载更多
    </button>
    <p v-else-if="!loading" class="muted">还没有执行记录。请先从流程列表选择一个固定流程。</p>

    <template v-if="selected && history">
      <h2>根与分支</h2>
      <p class="muted">根执行：{{ history.root_run_id.slice(0, 8) }}</p>
      <ul>
        <li v-for="entry in history.items" :key="entry.id">
          {{ entry.id.slice(0, 8) }}：{{ entry.status }}
          <span v-if="entry.parent_run_id" class="muted">
            （父 {{ entry.parent_run_id.slice(0, 8) }}）
          </span>
          <span v-if="entry.fork_step_id" class="muted">（起点 {{ entry.fork_step_id }}）</span>
          <span class="muted">（文档 {{ entry.artifact_ids.length }} 份）</span>
          <ul>
            <li v-for="step in entry.steps" :key="step.step_id">
              {{ step.step_id }}：{{ step.status }}
              <span v-if="step.selected_transition" class="muted">
                → {{ step.selected_transition }}
              </span>
            </li>
          </ul>
        </li>
      </ul>

      <SideEffectsPanel :key="`writes-${selected.id}`" :run-id="selected.id" />
      <BranchProposalPanel
        :key="selected.id"
        :parent-run-id="selected.id"
        :steps="steps"
        :proposal-id="typeof route.query.proposal === 'string' ? route.query.proposal : undefined"
        @created="onBranchCreated"
      />
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

button.link {
  background: none;
  border: none;
  padding: 0;
  margin-inline-end: 0.5rem;
  color: var(--pa-accent);
  cursor: pointer;
  text-decoration: underline;
}
</style>
