/**
 * Personal-Agent runtime guard.
 *
 * This is a permission mechanism only. It never schedules business steps, never
 * writes business state and never implements an Agent Loop: the official
 * Harness Loop owns the node-local loop, and the Python engine still validates
 * scope, epoch and budget again on every call.
 *
 * Two layers are required because the official tool service applies `restrict`
 * to inherited tools while tools registered in the plugin's own scope can
 * bypass that filter. `restrict` narrows the roster; `guard` rejects every
 * invocation whose tool is outside the profile allowlist, including one
 * registered later in the same scope.
 */

export type GuardCode =
  | 'TOOL_NOT_ALLOWED'
  | 'MANAGEMENT_API_DENIED'
  | 'SCOPE_TOKEN_MISSING'
  | 'EPOCH_MISMATCH'
  | 'DOMAIN_LIST_EMPTY'
  | 'MODEL_TARGET_NOT_ALLOWED'

export interface ToolPolicy {
  readonly allow: readonly string[]
  readonly denyPrefixes: readonly string[]
  readonly denyManagementApi: boolean
  readonly emptyDomainListDeniesNetwork: boolean
}

export interface GuardedToolCall {
  readonly tool: string
  readonly scopeToken?: string
  readonly epoch?: number
  readonly managementApi?: boolean
  /** Network calls declare the domains they intend to reach. */
  readonly domains?: readonly string[]
}

export interface GuardDecision {
  readonly allowed: boolean
  readonly code?: GuardCode
  readonly reason?: string
}

const ALLOW: GuardDecision = { allowed: true }

function deny(code: GuardCode, reason: string): GuardDecision {
  return { allowed: false, code, reason }
}

/** Normalise `fs.write` / `fs / write` / `FS.write` to comparable lowercase. */
export function normalizeToolName(name: string): string {
  return name.trim().toLowerCase().replace(/[\s/]+/g, '.')
}

export function createToolGuard(policy: ToolPolicy): (call: GuardedToolCall) => GuardDecision {
  const allow = new Set(policy.allow.map(normalizeToolName))
  const denyPrefixes = policy.denyPrefixes.map(normalizeToolName)

  return (call: GuardedToolCall): GuardDecision => {
    if (policy.denyManagementApi && call.managementApi === true) {
      return deny('MANAGEMENT_API_DENIED', 'tools may not call Personal-Agent management APIs')
    }

    const tool = normalizeToolName(call.tool)
    if (tool.length === 0) {
      return deny('TOOL_NOT_ALLOWED', 'empty tool name')
    }
    // Deny wins over allow: a denied capability is never re-enabled by a later
    // registration in the same scope.
    if (denyPrefixes.some((prefix) => tool === prefix || tool.startsWith(`${prefix}.`) || tool.startsWith(prefix))) {
      return deny('TOOL_NOT_ALLOWED', `${tool} is forbidden by the restricted profile`)
    }
    if (!allow.has(tool)) {
      return deny('TOOL_NOT_ALLOWED', `${tool} is not in the profile allowlist`)
    }
    if (typeof call.scopeToken !== 'string' || call.scopeToken.trim().length === 0) {
      return deny('SCOPE_TOKEN_MISSING', 'node scope token is required')
    }
    if (call.epoch !== undefined && (!Number.isInteger(call.epoch) || call.epoch < 1)) {
      return deny('EPOCH_MISMATCH', 'epoch must be a positive integer')
    }
    if (policy.emptyDomainListDeniesNetwork && call.domains !== undefined && call.domains.length === 0) {
      return deny('DOMAIN_LIST_EMPTY', 'an empty domain list denies network access instead of allowing all')
    }
    return ALLOW
  }
}

/** Minimal surface of the official Cordis tool service used by this plugin. */
export interface ToolServiceLike {
  restrict?(options: { allow: readonly string[] }): (() => void) | void
  // dsh-tools 0.2.1-rc.2: ToolExecution.name, string = deny, undefined = allow.
  guard?(predicate: (execution: { readonly name: string }) => string | undefined): () => void
  register?(definition: { name: string }, handler: unknown): void
  /** Global lookup; a name that is not registered yet cannot be restricted. */
  get?(name: string): unknown
}

export interface InstalledGuard {
  decision(call: GuardedToolCall): GuardDecision
  dispose(): void
}

/**
 * Installs `restrict` plus `guard`. Returns a disposer so a profile reload can
 * drop the previous predicate instead of stacking guards.
 */
export function installGuard(
  tools: ToolServiceLike,
  policy: ToolPolicy,
  // Bound by the trusted node session, never taken from model tool arguments.
  scope: { readonly scopeToken?: string; readonly epoch?: number } = {},
): InstalledGuard {
  const decide = createToolGuard(policy)
  if (typeof tools.restrict !== 'function' || typeof tools.guard !== 'function') {
    throw new Error('the pinned Harness restrict and guard services are required')
  }
  // The pinned service refuses unknown global names. Restrict only the tools
  // that exist now; the guard below still rejects every name outside the
  // allowlist, including one registered later in this scope.
  const known = policy.allow.filter(
    (name) => typeof tools.get !== 'function' || tools.get(name) !== undefined,
  )
  const releaseRestriction =
    known.length > 0 ? tools.restrict({ allow: known.map(normalizeToolName) }) : undefined
  let releaseGuard: () => void
  try {
    releaseGuard = tools.guard((execution) => {
      const decision = decide({ tool: execution.name, ...scope })
      return decision.allowed ? undefined : decision.code ?? 'TOOL_NOT_ALLOWED'
    })
  } catch (error) {
    releaseRestriction?.()
    throw error
  }
  return {
    decision: decide,
    dispose: () => { releaseGuard(); releaseRestriction?.() },
  }
}

export interface ModelTransportTarget {
  readonly provider: string
  readonly baseUrl: string
}

/**
 * The runtime may only talk to the local gateway for an already configured
 * provider. Arbitrary proxy targets are rejected so the gateway cannot be
 * bypassed by editing one request.
 */
export function createModelTransportHook(options: {
  readonly providers: readonly string[]
  readonly gatewayBaseUrl: string
}): (target: ModelTransportTarget) => GuardDecision {
  const providers = new Set(options.providers.map((p) => p.trim().toLowerCase()))
  return (target) => {
    if (!providers.has(target.provider.trim().toLowerCase())) {
      return deny('MODEL_TARGET_NOT_ALLOWED', `provider ${target.provider} is not configured`)
    }
    const url = target.baseUrl.trim().toLowerCase().replace(/\/+$/, '')
    const gateway = options.gatewayBaseUrl.trim().toLowerCase().replace(/\/+$/, '')
    if (url !== gateway) {
      return deny('MODEL_TARGET_NOT_ALLOWED', 'model transport must target the local gateway')
    }
    return ALLOW
  }
}

export const PLUGIN_NAME = 'personal-agent-runtime-policy'
