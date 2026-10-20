<script setup lang="ts">
/**
 * First-run setup: configuration health, policy acknowledgement and credential
 * health. `completed` is derived by the server; this page only records an explicit
 * acknowledgement. No account, login or role switching is required.
 */
import { computed, onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface Credential {
  provider: string
  configured: boolean
  source: string | null
  fingerprint: string | null
  last_checked_at: string
  error_code: string | null
  env_var: string
}

interface SetupStatus {
  completed: boolean
  required: boolean
  missing: string[]
  migrations: { current: string | null; head: string | null; up_to_date: boolean }
  data_dir: { writable: boolean; path: string }
  runtime: { available: boolean; compatible: boolean; execution_available: boolean }
  policy: { version: string; hash: string; acknowledged: boolean; acknowledged_at: string | null }
  credentials: { items: Credential[]; checked_at: string }
}

const client = new ApiClient()
const status = ref<SetupStatus | null>(null)
const error = ref<string | null>(null)
const message = ref<string | null>(null)
const busy = ref(false)
const dataAck = ref(false)
const budgetAck = ref(false)
const explicit = ref(false)

const canAcknowledge = computed(
  () => status.value !== null && dataAck.value && budgetAck.value && explicit.value && !busy.value,
)

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '读取失败。'
}

async function load(): Promise<void> {
  try {
    status.value = await client.get<SetupStatus>('/api/v1/setup/status')
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  }
}

async function acknowledge(): Promise<void> {
  if (!status.value) return
  busy.value = true
  error.value = null
  try {
    status.value = await client.post<SetupStatus>('/api/v1/setup/acknowledgements', {
      policy_version: status.value.policy.version,
      policy_hash: status.value.policy.hash,
      data_policy_acknowledged: dataAck.value,
      budget_policy_acknowledged: budgetAck.value,
      explicit_confirm: explicit.value,
    })
    message.value = '政策确认已记录；完成状态仍由服务端校验。'
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function checkEnvironment(provider: string): Promise<void> {
  busy.value = true
  error.value = null
  try {
    await client.put(`/api/v1/settings/credentials/${provider}`, {
      source: 'ENV',
      secret: null,
      explicit_save: true,
    })
    await load()
    message.value = '已检查环境变量中的凭据。'
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <section>
    <h1>首次配置</h1>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="!status" class="muted">加载中…</p>

    <template v-if="status">
      <p :class="status.completed ? '' : 'warn'">
        {{ status.completed ? '配置已完成。' : '配置尚未完成：' + status.missing.join('；') }}
      </p>

      <h2>环境检查</h2>
      <dl class="summary">
        <dt>数据库迁移</dt>
        <dd>
          {{ status.migrations.current ?? '未知' }} / {{ status.migrations.head ?? '未知' }}
          <span v-if="!status.migrations.up_to_date" class="warn">（需要迁移）</span>
        </dd>
        <dt>数据目录</dt>
        <dd>
          {{ status.data_dir.path }}
          <span v-if="!status.data_dir.writable" class="error">（不可写）</span>
        </dd>
        <dt>运行时</dt>
        <dd>
          {{ status.runtime.available ? '已检测' : '不可用' }} ·
          {{ status.runtime.execution_available ? '可开始研究' : '执行未启用' }}
        </dd>
      </dl>

      <h2>政策确认</h2>
      <p class="muted">版本 {{ status.policy.version }} · {{ status.policy.hash }}</p>
      <p v-if="status.policy.acknowledged">已于 {{ status.policy.acknowledged_at }} 确认。</p>
      <label class="inline">
        <input v-model="dataAck" type="checkbox" />
        我已阅读数据保留与删除政策
      </label>
      <label class="inline">
        <input v-model="budgetAck" type="checkbox" />
        我已阅读费用与额度政策
      </label>
      <label class="inline">
        <input v-model="explicit" type="checkbox" />
        我明确提交以上确认
      </label>
      <button type="button" :disabled="!canAcknowledge" @click="acknowledge">记录确认</button>

      <h2>凭据健康</h2>
      <table>
        <thead>
          <tr>
            <th scope="col">用途</th>
            <th scope="col">状态</th>
            <th scope="col">来源</th>
            <th scope="col">检查时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="credential in status.credentials.items" :key="credential.provider">
            <td>{{ credential.provider }}</td>
            <td>
              {{ credential.configured ? '已配置' : '未配置' }}
              <span v-if="credential.error_code" class="muted">（{{ credential.error_code }}）</span>
            </td>
            <td>
              {{ credential.source ?? '—' }}
              <span class="muted">（环境变量 {{ credential.env_var }}）</span>
            </td>
            <td>{{ credential.last_checked_at }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted">
        凭据只显示健康状态与指纹前缀；系统凭据存储不可用时不会假装保存。页面不会回显密钥。
      </p>
      <div class="buttons">
        <button
          v-for="credential in status.credentials.items"
          :key="`check-${credential.provider}`"
          type="button"
          :disabled="busy"
          @click="checkEnvironment(credential.provider)"
        >
          检查 {{ credential.provider }} 环境变量
        </button>
      </div>
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

.inline {
  display: inline-flex;
  gap: 0.25rem;
  align-items: center;
  margin-inline-end: 1rem;
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
}

.buttons {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
</style>
