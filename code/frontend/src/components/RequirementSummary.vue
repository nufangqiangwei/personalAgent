<script setup lang="ts">
import SafeMarkdown from '@/components/SafeMarkdown.vue'

interface FieldEntry {
  path: string
  value: unknown
  source: string
  accepted?: boolean
}

const props = defineProps<{
  workflowName: string
  workflowVersionNo: number
  summaryMarkdown: string
  fields: FieldEntry[]
  missingFields: string[]
  conflicts: { path?: string; message?: string }[]
}>()

const SOURCE_LABEL: Record<string, string> = {
  user: '你提供',
  default: '默认值（待确认）',
  suggestion: '已接受建议',
}

function format(value: unknown): string {
  if (value === null || value === undefined) return '无特别偏好'
  if (Array.isArray(value)) return value.join('；')
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}
</script>

<template>
  <section class="summary" aria-label="需求完整摘要">
    <SafeMarkdown :text="summaryMarkdown" />

    <h3>字段来源</h3>
    <table v-if="fields.length > 0">
      <thead>
        <tr>
          <th scope="col">字段</th>
          <th scope="col">值</th>
          <th scope="col">来源</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="field in fields" :key="field.path">
          <td>{{ field.path }}</td>
          <td>{{ format(field.value) }}</td>
          <td>{{ SOURCE_LABEL[field.source] ?? field.source }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">尚未收集到任何要求。</p>

    <p v-if="missingFields.length > 0" class="error">
      仍缺少：{{ missingFields.join('、') }}
    </p>
    <ul v-if="conflicts.length > 0" class="error">
      <li v-for="(conflict, index) in conflicts" :key="index">
        冲突：{{ conflict.path ?? '未知字段' }} — {{ conflict.message ?? '' }}
      </li>
    </ul>
    <p class="muted">
      流程版本 v{{ workflowVersionNo }} · {{ workflowName }}。确认后才会创建执行。
    </p>
  </section>
</template>

<style scoped>
table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  padding: 0.35rem;
  border-bottom: 1px solid var(--pa-border);
  text-align: left;
}
</style>
