<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  versionId: string
  definition: unknown
  saving: boolean
  errorMessage: string | null
}>()

const emit = defineEmits<{
  save: [
    payload: {
      definition: Record<string, unknown>
      changeSummary: string
      confirmExecutable: boolean
    },
  ]
}>()

const text = ref(JSON.stringify(props.definition, null, 2))
const changeSummary = ref('')
const confirmExecutable = ref(false)

watch(
  () => props.versionId,
  () => {
    text.value = JSON.stringify(props.definition, null, 2)
    changeSummary.value = ''
    confirmExecutable.value = false
  },
)

const parseError = computed<string | null>(() => {
  try {
    const parsed = JSON.parse(text.value) as unknown
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return '定义必须是一个 JSON 对象。'
    }
    return null
  } catch (error) {
    return `JSON 解析失败：${error instanceof Error ? error.message : String(error)}`
  }
})

const canSave = computed(
  () => parseError.value === null && changeSummary.value.trim().length > 0 && !props.saving,
)

function save(): void {
  if (!canSave.value) return
  emit('save', {
    definition: JSON.parse(text.value) as Record<string, unknown>,
    changeSummary: changeSummary.value.trim(),
    confirmExecutable: confirmExecutable.value,
  })
}
</script>

<template>
  <section class="editor" aria-label="流程定义编辑器">
    <p class="muted">
      持久格式是规范 JSON。保存会创建不可变版本；只有勾选“确认为可执行”且校验通过，才会成为最新可执行版本。
    </p>
    <label for="definition-json">定义 JSON</label>
    <textarea
      id="definition-json"
      v-model="text"
      spellcheck="false"
      rows="24"
      :disabled="saving"
    />
    <p v-if="parseError" class="error" role="alert">{{ parseError }}</p>

    <label for="change-summary">变更说明</label>
    <input
      id="change-summary"
      v-model="changeSummary"
      type="text"
      maxlength="1000"
      placeholder="例如：调整运维限制方向的输出"
      :disabled="saving"
    />

    <label class="checkbox">
      <input v-model="confirmExecutable" type="checkbox" :disabled="saving" />
      确认为可执行版本
    </label>

    <p v-if="errorMessage" class="error" role="alert">{{ errorMessage }}</p>

    <button type="button" :disabled="!canSave" @click="save">
      {{ saving ? '保存中…' : '保存为新版本' }}
    </button>
  </section>
</template>

<style scoped>
.editor {
  display: grid;
  gap: 0.6rem;
}

textarea {
  width: 100%;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 0.85rem;
  line-height: 1.45;
}

input[type='text'] {
  width: 100%;
  padding: 0.4rem;
  font: inherit;
}

.checkbox {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
</style>
