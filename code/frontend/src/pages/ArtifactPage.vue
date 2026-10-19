<script setup lang="ts">
/**
 * Delivered document page.
 *
 * The page reads the document metadata and its Markdown, then shows the real
 * completeness (a PARTIAL document is labelled partial, never complete), the
 * version that produced it, the reasons behind each candidate, the gaps and the
 * sources. "再次获取" only re-reads the stored document: it never restarts the
 * research, and a failed read is reported as unavailable instead of delivered.
 */
import { computed, onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import SourceList, { type ArtifactSource } from '@/components/SourceList.vue'

interface Candidate {
  id?: string
  name?: string
  reason?: string
  constraints?: { constraint?: string; result?: string; evidence?: string[] }[]
  source_ids?: string[]
}

interface Direction {
  id: string
  title?: string
  status: string
  findings?: { claim?: string; source_ids?: string[]; validity?: string }[]
  gaps?: string[]
  source_ids?: string[]
}

interface DocumentContent {
  schema_version: number
  title: string
  structure: string
  completeness: string
  objective: string
  constraints: string[]
  directions: Direction[]
  qualified: Candidate[]
  unknown_candidates: Candidate[]
  rejected_candidates: Candidate[]
  sources: ArtifactSource[]
  gaps: string[]
  unverified: { claim?: string; reason?: string }[]
  notes: string[]
}

interface ArtifactMetadata {
  id: string
  run_id: string
  workflow_version_id: string
  kind: string
  format: string
  structure: string
  completeness: string
  manifest_hash: string
  created_at: string
  access_state: string
  size_bytes: number
  sha256: string
  document: DocumentContent
}

const props = defineProps<{ id: string; artifactId: string }>()

const client = new ApiClient()

const metadata = ref<ArtifactMetadata | null>(null)
const markdown = ref('')
const error = ref<string | null>(null)
const unavailable = ref(false)
const loading = ref(true)
const fetching = ref(false)
const reread = ref(false)

const COMPLETENESS_LABELS: Record<string, string> = {
  COMPLETE: '完整',
  PARTIAL: '部分完成（存在缺口）',
  NO_QUALIFIED_RESULT: '没有满足全部硬约束的结果',
}

const STRUCTURE_LABELS: Record<string, string> = {
  report: '报告',
  checklist: '检查清单',
  comparison: '对比',
}

const document = computed<DocumentContent | null>(() => metadata.value?.document ?? null)
const delivered = computed(() => !unavailable.value && markdown.value.length > 0)
const completeness = computed(() => metadata.value?.completeness ?? '')
const completenessLabel = computed(
  () => COMPLETENESS_LABELS[completeness.value] ?? completeness.value,
)
const structureLabel = computed(
  () => STRUCTURE_LABELS[metadata.value?.structure ?? ''] ?? metadata.value?.structure ?? '',
)
const filename = computed(
  () => `personal-agent-${props.id.slice(0, 8)}-${props.artifactId.slice(0, 8)}.md`,
)

function describe(caught: unknown): string {
  return caught instanceof ApiError
    ? messageFor(caught.code, caught.message)
    : '读取文档失败。'
}

function isUnavailable(caught: unknown): boolean {
  return caught instanceof ApiError && caught.code === 'ARTIFACT_UNAVAILABLE'
}

async function loadContent(): Promise<boolean> {
  try {
    markdown.value = await client.get<string>(`/api/v1/artifacts/${props.artifactId}/content`)
    unavailable.value = false
    return true
  } catch (caught) {
    markdown.value = ''
    unavailable.value = isUnavailable(caught)
    error.value = unavailable.value
      ? '文档当前不可访问（已删除、已过期或内容校验失败），不能视为已交付。'
      : describe(caught)
    return false
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    metadata.value = await client.get<ArtifactMetadata>(`/api/v1/artifacts/${props.artifactId}`)
    error.value = null
    await loadContent()
  } catch (caught) {
    metadata.value = null
    unavailable.value = isUnavailable(caught)
    error.value = unavailable.value
      ? '文档当前不可访问（已删除或已过期），不能视为已交付。'
      : describe(caught)
  } finally {
    loading.value = false
  }
}

/** Re-read the stored document only; the research itself is never re-run. */
async function rereadContent(): Promise<void> {
  if (fetching.value) return
  fetching.value = true
  reread.value = false
  try {
    error.value = null
    const ok = await loadContent()
    reread.value = ok
  } finally {
    fetching.value = false
  }
}

function save(): void {
  if (!delivered.value) return
  const blob = new Blob([markdown.value], { type: 'text/markdown;charset=utf-8' })
  const createObjectUrl = globalThis.URL?.createObjectURL
  if (typeof createObjectUrl !== 'function') return
  const url = createObjectUrl.call(globalThis.URL, blob)
  const anchor = globalThis.document.createElement('a')
  anchor.href = url
  anchor.download = filename.value
  anchor.rel = 'noopener'
  globalThis.document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  const revokeObjectUrl = globalThis.URL?.revokeObjectURL
  if (typeof revokeObjectUrl === 'function') revokeObjectUrl.call(globalThis.URL, url)
}

onMounted(load)
</script>

<template>
  <section>
    <h1>文档</h1>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <template v-if="metadata">
      <dl class="summary">
        <dt>完成程度</dt>
        <dd>
          <strong :class="{ warn: completeness !== 'COMPLETE' }">{{ completenessLabel }}</strong>
          <span class="muted">（{{ completeness }}）</span>
        </dd>
        <dt>结构</dt>
        <dd>{{ structureLabel }}</dd>
        <dt>实际版本</dt>
        <dd>{{ metadata.workflow_version_id }}</dd>
        <dt>生成时间</dt>
        <dd>{{ metadata.created_at }}</dd>
        <dt>可访问状态</dt>
        <dd :class="{ warn: metadata.access_state !== 'AVAILABLE' }">
          {{ metadata.access_state }}
        </dd>
      </dl>

      <p v-if="completeness === 'PARTIAL'" class="warn">
        本次为部分交付：仍存在缺口，缺口与未证实内容已逐条列出，不能当作完整结果使用。
      </p>
      <p v-if="completeness === 'NO_QUALIFIED_RESULT'" class="warn">
        本次没有满足全部硬约束的结果，不提供合格推荐。
      </p>

      <div class="actions">
        <button type="button" :disabled="fetching" @click="rereadContent">
          {{ fetching ? '再次获取中…' : '再次获取' }}
        </button>
        <button type="button" :disabled="!delivered" @click="save">保存</button>
      </div>
      <p v-if="reread" class="muted" role="status">已重新读取同一份文档，未重新运行研究。</p>

      <template v-if="document">
        <h2>研究目标</h2>
        <p>{{ document.objective || '未记录目标' }}</p>

        <h2 v-if="document.constraints.length > 0">硬约束</h2>
        <ul v-if="document.constraints.length > 0">
          <li v-for="constraint in document.constraints" :key="constraint">{{ constraint }}</li>
        </ul>

        <h2>查询方向与结果</h2>
        <ul class="directions">
          <li v-for="direction in document.directions" :key="direction.id">
            <strong>{{ direction.title ?? direction.id }}</strong>
            <span v-if="direction.status === 'covered'" class="muted">（已覆盖）</span>
            <span v-else class="warn">（缺少该方向的结果）</span>
            <ul v-if="direction.gaps && direction.gaps.length > 0">
              <li v-for="gap in direction.gaps" :key="gap" class="warn">{{ gap }}</li>
            </ul>
          </li>
        </ul>

        <h2>推荐理由</h2>
        <p v-if="document.qualified.length === 0" class="muted">
          没有满足全部硬约束的候选，不提供合格推荐。
        </p>
        <ul class="reasons">
          <li v-for="candidate in document.qualified" :key="candidate.id ?? candidate.name">
            <strong>{{ candidate.name ?? candidate.id }}</strong>：满足全部硬约束
            <ul>
              <li
                v-for="constraint in candidate.constraints ?? []"
                :key="constraint.constraint ?? ''"
              >
                {{ constraint.constraint }} — {{ constraint.result }}
              </li>
            </ul>
          </li>
          <li v-for="candidate in document.unknown_candidates" :key="`unknown-${candidate.id}`">
            <strong>{{ candidate.name ?? candidate.id }}</strong>：待确认原因
            {{ candidate.reason ?? '存在未知硬约束' }}
            <ul>
              <li
                v-for="constraint in candidate.constraints ?? []"
                :key="constraint.constraint ?? ''"
              >
                {{ constraint.constraint }} — {{ constraint.result }}
              </li>
            </ul>
          </li>
          <li v-for="candidate in document.rejected_candidates" :key="`rejected-${candidate.id}`">
            <strong>{{ candidate.name ?? candidate.id }}</strong>：不满足硬约束
            <ul>
              <li
                v-for="constraint in candidate.constraints ?? []"
                :key="constraint.constraint ?? ''"
              >
                {{ constraint.constraint }} — {{ constraint.result }}
              </li>
            </ul>
          </li>
        </ul>

        <h2>缺口与未证实</h2>
        <ul v-if="document.gaps.length > 0" class="gaps">
          <li v-for="gap in document.gaps" :key="gap">{{ gap }}</li>
        </ul>
        <p v-else class="muted">无。</p>
        <ul v-if="document.unverified.length > 0">
          <li v-for="item in document.unverified" :key="item.claim ?? ''" class="warn">
            {{ item.claim }}（{{ item.reason ?? '未证实' }}）
          </li>
        </ul>

        <h2>来源</h2>
        <SourceList :sources="document.sources" />
      </template>

      <h2>文档正文</h2>
      <SafeMarkdown v-if="delivered" :text="markdown" />
      <p v-else-if="!unavailable" class="muted">正文尚未读取。</p>
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

.error {
  color: var(--pa-warn);
}

.actions {
  display: flex;
  gap: 0.5rem;
  margin: 1rem 0;
}

ul {
  padding-left: 1.25rem;
}
</style>
