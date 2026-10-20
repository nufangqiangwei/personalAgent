<script setup lang="ts">
/**
 * Maintenance: stop new actions, back up, verify, restore and migrate.
 *
 * Entering never claims "stopped" while an action is in flight. A backup or restore is
 * refused until quiescent; restoring keeps maintenance mode and never auto-resumes a
 * run. Every risky action needs an explicit confirmation.
 */
import { onMounted, ref } from 'vue'

import { ApiClient, ApiError, messageFor } from '@/api/client'

interface MaintenanceStatus {
  mode: string
  revision: number
  reason: string | null
  draining: boolean
  quiescent: boolean
  in_flight_actions: { id: string; type: string; status: string }[]
  pending_writes: { id: string; status: string; target_ref: string }[]
  unknown_model_request: { id: string; status: string } | null
  migration_version: string | null
  migration_head: string | null
  backup_in_progress: boolean
  restore_in_progress: boolean
  allowed_actions: { action: string; enabled: boolean; disabled_reason: string | null }[]
}

interface Backup {
  id: string
  status: string
  usable: boolean
  restore_allowed: boolean
  manifest_hash: string
  schema_version: string
  blob_count: number
  created_at: string
  expires_at: string
}

const client = new ApiClient()
const status = ref<MaintenanceStatus | null>(null)
const backups = ref<Backup[]>([])
const reason = ref('升级或维护演练')
const error = ref<string | null>(null)
const message = ref<string | null>(null)
const busy = ref(false)
const selectedBackup = ref<Backup | null>(null)
const acknowledgeRestore = ref(false)

function describe(caught: unknown): string {
  return caught instanceof ApiError ? messageFor(caught.code, caught.message) : '操作失败。'
}

function action(name: string) {
  return status.value?.allowed_actions.find((item) => item.action === name)
}

async function load(): Promise<void> {
  try {
    status.value = await client.get<MaintenanceStatus>('/api/v1/maintenance/status')
    backups.value = (await client.get<{ items: Backup[] }>('/api/v1/maintenance/backups')).items
    error.value = null
  } catch (caught) {
    error.value = describe(caught)
  }
}

async function enter(): Promise<void> {
  if (!status.value) return
  busy.value = true
  try {
    await client.post(
      '/api/v1/maintenance/enter',
      { reason: reason.value, explicit_confirm: true },
      { etag: `"${status.value.revision}"` },
    )
    message.value = '已请求进入维护；收敛后才会显示为已停止。'
    await load()
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function exit(): Promise<void> {
  if (!status.value) return
  busy.value = true
  try {
    status.value = await client.post<MaintenanceStatus>(
      '/api/v1/maintenance/exit',
      { expected_revision: status.value.revision, explicit_confirm: true },
      { etag: `"${status.value.revision}"` },
    )
    message.value = '已退出维护。遗留执行保持中断，不会自动续跑。'
    await load()
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function createBackup(): Promise<void> {
  busy.value = true
  try {
    await client.post('/api/v1/maintenance/backups', {
      reason: reason.value,
      explicit_backup: true,
    })
    message.value = '备份已创建并验证。'
    await load()
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function verify(backup: Backup): Promise<void> {
  busy.value = true
  try {
    await client.post(
      `/api/v1/maintenance/backups/${backup.id}/verify`,
      { manifest_hash: backup.manifest_hash },
    )
    message.value = '备份已重新核验。'
    await load()
  } catch (caught) {
    error.value = describe(caught)
  } finally {
    busy.value = false
  }
}

async function restore(): Promise<void> {
  if (!selectedBackup.value) return
  if (!acknowledgeRestore.value) {
    error.value = '恢复需要勾选影响确认。'
    return
  }
  busy.value = true
  try {
    await client.post('/api/v1/maintenance/restores', {
      backup_id: selectedBackup.value.id,
      manifest_hash: selectedBackup.value.manifest_hash,
      explicit_restore: true,
      acknowledge_interruption: true,
      acknowledge_lifecycle: true,
    })
    message.value = '恢复完成；维护模式保持，执行保持中断。'
    await load()
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
    <h1>维护与备份</h1>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="!status" class="muted">加载中…</p>

    <template v-if="status">
      <dl class="summary">
        <dt>模式</dt>
        <dd>
          {{ status.mode }}
          <span v-if="status.draining" class="warn">（收敛中）</span>
        </dd>
        <dt>静默</dt>
        <dd>{{ status.quiescent ? '是' : '否' }}</dd>
        <dt>迁移</dt>
        <dd>{{ status.migration_version ?? '未知' }} / {{ status.migration_head ?? '未知' }}</dd>
        <dt>在途动作</dt>
        <dd>{{ status.in_flight_actions.length }} 个</dd>
        <dt>未核验写入</dt>
        <dd>{{ status.pending_writes.length }} 个</dd>
        <dt>未决模型请求</dt>
        <dd>{{ status.unknown_model_request?.status ?? '无' }}</dd>
      </dl>

      <label>
        原因
        <input v-model="reason" type="text" maxlength="2000" />
      </label>
      <div class="buttons">
        <button
          type="button"
          :disabled="busy || !action('ENTER_MAINTENANCE')?.enabled"
          @click="enter"
        >
          进入维护
        </button>
        <button
          type="button"
          :disabled="busy || status.mode !== 'MAINTENANCE' || !status.quiescent"
          @click="createBackup"
        >
          创建备份
        </button>
        <button
          type="button"
          :disabled="busy || !action('EXIT_MAINTENANCE')?.enabled"
          @click="exit"
        >
          退出维护
        </button>
      </div>

      <h2>备份</h2>
      <table v-if="backups.length > 0">
        <thead>
          <tr>
            <th scope="col">备份</th>
            <th scope="col">状态</th>
            <th scope="col">版本</th>
            <th scope="col">文件数</th>
            <th scope="col">到期</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="backup in backups" :key="backup.id">
            <td>{{ backup.id.slice(0, 8) }}</td>
            <td>
              {{ backup.status }}
              <span v-if="!backup.usable" class="muted">（不可恢复）</span>
            </td>
            <td>{{ backup.schema_version }}</td>
            <td>{{ backup.blob_count }}</td>
            <td>{{ backup.expires_at }}</td>
            <td>
              <button type="button" :disabled="busy" @click="verify(backup)">核验</button>
              <button
                type="button"
                :disabled="busy || !backup.restore_allowed"
                @click="selectedBackup = backup"
              >
                选择恢复
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有备份。请先进入维护并等待静默。</p>

      <template v-if="selectedBackup">
        <h2>恢复 {{ selectedBackup.id.slice(0, 8) }}</h2>
        <p class="muted">
          恢复会替换当前数据；删除墓碑会重新应用，遗留执行保持中断且不会自动继续。
        </p>
        <label class="inline">
          <input v-model="acknowledgeRestore" type="checkbox" />
          我确认恢复会中断在途执行并应用保留政策
        </label>
        <button type="button" :disabled="busy || !acknowledgeRestore" @click="restore">
          执行恢复
        </button>
      </template>
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

.buttons {
  display: flex;
  gap: 0.5rem;
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
