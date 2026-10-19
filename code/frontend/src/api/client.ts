/**
 * Typed API client for the local Personal-Agent backend.
 *
 * Responsibilities kept in one place so pages cannot diverge:
 * - attach the local CSRF header to unsafe requests;
 * - generate an Idempotency-Key for every write and let callers pin one;
 * - send If-Match from the revision ETag and surface 412 as a stale conflict;
 * - map structured errors to a stable ApiError and a user-facing message.
 */

export interface ApiErrorPayload {
  code: string
  message: string
  retryable: boolean
  details: Record<string, unknown>
  request_id: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly details: Record<string, unknown>
  readonly requestId: string

  constructor(status: number, payload: ApiErrorPayload) {
    super(payload.message)
    this.name = 'ApiError'
    this.status = status
    this.code = payload.code
    this.retryable = payload.retryable
    this.details = payload.details
    this.requestId = payload.request_id
  }
}

export const ERROR_MESSAGES: Record<string, string> = {
  INPUT_INCOMPLETE: '必要信息还未补齐，请继续补充后再确认。',
  UNACCEPTED_SUGGESTION: '还有未接受的建议，不能直接确认。',
  CONFIRMATION_STALE: '要求摘要已变化，请重新确认。',
  VERSION_CHANGED: '工作流版本已变化，请重新选择并确认。',
  RUN_BUSY: '已有执行在运行，请等待它结束或先处理当前执行。',
  MODEL_BUSY: '模型正忙，请稍后重试；聊天内容已保留。',
  MODEL_OUTCOME_UNKNOWN: '上一次模型结果无法确认，解决前不能继续调用模型。',
  PREREQUISITE_INVALID: '所选起点缺少有效的前置成果。',
  START_POINT_REQUIRES_CONFIRMATION: '起点影响无法确定，请确认更早的起点。',
  BUDGET_ASSISTANCE_REQUIRED: '预算已触顶，需要你明确追加后才能继续。',
  DRAFT_BUDGET_NOT_EXHAUSTED: '草稿额度尚未触顶，不需要追加。',
  DRAFT_BUDGET_GRANT_INVALID: '追加额度的数值不合法。',
  DRAFT_BUDGET_CONFLICT: '额度被并发修改，请重新读取后再试。',
  DRAFT_STATE_INVALID: '当前草稿状态不允许该操作。',
  SETUP_REQUIRED: '请先完成首次配置。',
  SETUP_INCOMPLETE: '首次配置或运行环境检查未通过，写操作已暂停。',
  POLICY_CHANGED: '保留与上限政策已更新，请重新阅读并确认。',
  POLICY_ACKNOWLEDGEMENT_REQUIRED: '请先阅读并明确确认政策。',
  CREDENTIAL_INVALID: '凭据配置不合法。',
  CREDENTIAL_UNAVAILABLE: '系统凭据存储不可用。',
  MAINTENANCE_REQUIRED: '该操作只能在维护模式下进行。',
  MAINTENANCE_BUSY: '维护操作进行中或系统尚未静默。',
  MAINTENANCE_TRANSITION_BLOCKED: '仍有在途动作，暂不能切换维护状态。',
  MAINTENANCE_REVISION_STALE: '维护状态已变化，请重新读取。',
  BACKUP_NOT_FOUND: '找不到该备份。',
  BACKUP_INCOMPATIBLE: '该备份与当前版本不兼容。',
  BACKUP_VERIFICATION_FAILED: '备份验证失败，不能用于恢复。',
  BACKUP_EXPIRED: '该备份已过期。',
  BACKUP_DELETED: '该备份已删除。',
  MANIFEST_HASH_MISMATCH: '备份清单校验不一致。',
  RESTORE_BLOCKED: '当前条件不允许恢复。',
  RESTORE_CONFLICT: '已有恢复操作在进行。',
  SIDE_EFFECT_UNRESOLVED: '存在未核验的写入，需要先确认结果。',
  RUNTIME_INCOMPATIBLE: '运行时不兼容或尚未通过接入验证。',
  ARTIFACT_UNAVAILABLE: '文档当前不可访问。',
  DEFINITION_INVALID: '流程定义未通过校验。',
  CSRF_FAILED: '本地会话校验失败，请刷新页面后重试。',
  SESSION_REQUIRED: '本地会话缺失，请刷新页面重新建立会话。',
  IDEMPOTENCY_KEY_REQUIRED: '写请求缺少幂等键。',
  IDEMPOTENCY_CONFLICT: '同一幂等键被用于了不同的请求。',
  NETWORK_ERROR: '无法连接本机服务，请确认后端仍在运行。',
  REQUEST_INVALID: '请求内容不合法。',
  INTERNAL_ERROR: '服务发生内部错误，请查看日志后再试。',
}

export function messageFor(code: string, fallback?: string): string {
  return ERROR_MESSAGES[code] ?? fallback ?? '操作失败，请稍后重试。'
}

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

export interface RequestOptions {
  method?: string
  body?: unknown
  /** Current revision ETag; sent as If-Match for conditional updates. */
  etag?: string
  /** Pin a key to make a browser retry replay the original response. */
  idempotencyKey?: string
  headers?: Record<string, string>
  signal?: AbortSignal
}

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

export function newIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto as Crypto | undefined
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }
  return `pa-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const prefix = `${name}=`
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim()
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length))
    }
  }
  return null
}

export class ApiClient {
  private readonly baseUrl: string
  private readonly fetchImpl: FetchLike

  constructor(options: { baseUrl?: string; fetchImpl?: FetchLike } = {}) {
    this.baseUrl = options.baseUrl ?? ''
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init))
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = (options.method ?? 'GET').toUpperCase()
    const headers: Record<string, string> = {
      Accept: 'application/json',
      ...(options.headers ?? {}),
    }
    if (options.body !== undefined) headers['Content-Type'] = 'application/json'
    if (UNSAFE.has(method)) {
      headers['Idempotency-Key'] = options.idempotencyKey ?? newIdempotencyKey()
      const csrf = readCookie('pa_csrf')
      if (csrf) headers['X-CSRF-Token'] = csrf
    }
    if (options.etag) headers['If-Match'] = options.etag

    const init: RequestInit = { method, headers }
    if (options.body !== undefined) init.body = JSON.stringify(options.body)
    if (options.signal !== undefined) init.signal = options.signal

    let response: Response
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, init)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error
      throw new ApiError(0, {
        code: 'NETWORK_ERROR',
        message: messageFor('NETWORK_ERROR'),
        retryable: true,
        details: {},
        request_id: 'unavailable',
      })
    }

    if (response.ok) {
      if (response.status === 204) return undefined as T
      const contentType = response.headers.get('content-type') ?? ''
      if (contentType.includes('application/json')) return (await response.json()) as T
      return (await response.text()) as T
    }

    let payload: ApiErrorPayload
    try {
      const parsed = (await response.json()) as { error?: ApiErrorPayload }
      payload = parsed.error ?? {
        code: 'INTERNAL_ERROR',
        message: messageFor('INTERNAL_ERROR'),
        retryable: false,
        details: {},
        request_id: 'unavailable',
      }
    } catch {
      payload = {
        code: response.status === 412 ? 'CONFIRMATION_STALE' : 'INTERNAL_ERROR',
        message: messageFor(response.status === 412 ? 'CONFIRMATION_STALE' : 'INTERNAL_ERROR'),
        retryable: false,
        details: {},
        request_id: 'unavailable',
      }
    }
    throw new ApiError(response.status, payload)
  }

  get<T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'GET' })
  }

  post<T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'POST', body })
  }
}
