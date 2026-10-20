<script setup lang="ts">
/**
 * Stop and resume controls. Every button is enabled only from the server's
 * `allowed_actions`; a draining run shows its in-flight actions until it converges.
 */
import { computed, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'
import type { RunSnapshot, RunStore } from '@/stores/run'

const props = defineProps<{ runId: string; store: RunStore }>()
const client = new ApiClient()

const reason = ref('')
const cancelConfirmed = ref(false)
const busy = ref(false)
const message = ref<string | null>(null)
const error = ref<string | null>(null)

const run = computed<RunSnapshot | null>(() => props.store.state.run)
const pause = computed(() => props.store.action('PAUSE'))
const cancel = computed(() => props.store.action('CANCEL'))
const resume = computed(() => props.store.action('RESUME'))
const draining = computed(() => props.store.draining.value)
const inFlight = computed(() => props.store.inFlight.value)

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '操作失败。'
}

async function send(path: string, body: Record<string, unknown>): Promise<void> {
  if (run.value === null) return
  busy.value = true
  error.value = null
  message.value = null
  try {
    const snapshot = await client.post<RunSnapshot>(`/api/v1/runs/${props.runId}/${path}`, body, {
      etag: `"${run.value.revision}"`,
    })
    props.store.applySnapshot(snapshot)
    message.value = snapshot.draining
      ? '已记录停止意图；当前动作仍在收敛，完成后状态会更新。'
      : `状态已更新为 ${snapshot.status}。`
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

function pauseRun(): void {
  void send('pause', { revision: run.value?.revision ?? 1, reason: reason.value || null })
}

function resumeRun(): void {
  void send('resume', { revision: run.value?.revision ?? 1 })
}

function cancelRun(): void {
  if (!cancelConfirmed.value) {
    error.value = '取消需要勾选确认，避免误触。'
    return
  }
  void send('cancel', {
    revision: run.value?.revision ?? 1,
    reason: reason.value || null,
    confirmed: true,
  })
}
</script>

<template>
  <section class="controls">
    <h2>执行控制</h2>
    <p v-if="draining" class="warn" role="status">
      正在收敛：{{ inFlight.length }} 个动作仍在途，完成后才会停止。
    </p>
    <ul v-if="inFlight.length > 0" class="muted">
      <li v-for="action in inFlight" :key="action.id">
        {{ action.type }}（{{ action.status }}）
      </li>
    </ul>

    <label>
      原因（可选）
      <input v-model="reason" type="text" maxlength="500" />
    </label>

    <div class="buttons">
      <button type="button" :disabled="busy || !pause?.enabled" @click="pauseRun">
        暂停
      </button>
      <button type="button" :disabled="busy || !resume?.enabled" @click="resumeRun">
        继续（同 ID）
      </button>
      <label class="inline">
        <input v-model="cancelConfirmed" type="checkbox" />
        确认取消
      </label>
      <button type="button" :disabled="busy || !cancel?.enabled" @click="cancelRun">
        取消
      </button>
    </div>

    <p v-for="action in [pause, resume, cancel]" :key="action?.action" class="muted">
      <span v-if="action && !action.enabled">
        {{ action.action }} 不可用：{{ action.disabled_reason ?? '当前状态不允许' }}
      </span>
    </p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.controls {
  border: 1px solid var(--pa-border);
  padding: 1rem;
  margin-block: 1rem;
}

.buttons {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  flex-wrap: wrap;
  margin-block: 0.5rem;
}

.inline {
  display: inline-flex;
  gap: 0.25rem;
  align-items: center;
}

.warn {
  color: var(--pa-warn);
}
</style>
