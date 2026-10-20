import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  PLUGIN_NAME,
  createModelTransportHook,
  createToolGuard,
  installGuard,
  type InstalledGuard,
  type ToolPolicy,
  type ToolServiceLike,
} from './guard.js'

export * from './guard.js'

interface ProfileFile {
  scope?: { scopeToken: string; epoch: number }
  tools: {
    allow: string[]
    denyPrefixes: string[]
    denyManagementApi: boolean
    emptyDomainListDeniesNetwork: boolean
  }
  modelGateway: {
    providers: string[]
    gatewayBaseUrl?: string
  }
}

export function loadProfile(path?: string): ProfileFile {
  const file = path ?? fileURLToPath(new URL('../profile.json', import.meta.url))
  return JSON.parse(readFileSync(file, 'utf8')) as ProfileFile
}

export function toolPolicyFromProfile(profile: ProfileFile): ToolPolicy {
  return {
    allow: profile.tools.allow,
    denyPrefixes: profile.tools.denyPrefixes,
    denyManagementApi: profile.tools.denyManagementApi,
    emptyDomainListDeniesNetwork: profile.tools.emptyDomainListDeniesNetwork,
  }
}

/**
 * Cordis plugin entry. It mounts only the guard/provider hooks; business
 * orchestration stays in the Python engine and the official Loop.
 */
interface AgentScope {
  ctx: { tools: ToolServiceLike }
}

interface AgentEvent {
  agent: AgentScope
}

interface PolicyContext {
  tools: ToolServiceLike
  on(
    event: 'agent/created' | 'agent/disposed',
    listener: (payload: AgentEvent) => void,
  ): () => void
}

export function apply(ctx: PolicyContext, profile?: ProfileFile): InstalledGuard {
  // The pinned service rejects global restrict(). Mount on the published Agent
  // scope before queued model work is released by the official registry.
  // A missing inline config reads the deployment profile named by the launch
  // patch; the CLI never receives the profile inline, so its hash stays bound.
  const resolved = profile ?? loadProfile(process.env.PERSONAL_AGENT_PROFILE_PATH)
  const policy = toolPolicyFromProfile(resolved)
  // The launch patch binds the node scope to the per-attempt token/epoch; the
  // profile itself never carries a secret. The gateway re-validates the real
  // per-tool grant, so this token is only the "a trusted node started me" mark.
  const scope = resolved.scope ?? {
    scopeToken: process.env.PERSONAL_AGENT_GATEWAY_TOKEN ?? '',
    epoch: Number(process.env.PERSONAL_AGENT_SCOPE_EPOCH ?? '1'),
  }
  const installed = new Map<AgentScope, InstalledGuard>()
  // dsh-agent 0.2.1-rc.2 dispatches { agent, source, signal }, not the agent itself.
  const created = ctx.on('agent/created', event => {
    const agent = event.agent
    if (!installed.has(agent)) {
      installed.set(agent, installGuard(agent.ctx.tools, policy, scope))
    }
  })
  const disposed = ctx.on('agent/disposed', event => {
    const agent = event.agent
    installed.get(agent)?.dispose()
    installed.delete(agent)
  })
  return {
    decision: createToolGuard(policy),
    dispose: () => {
      created()
      disposed()
      for (const guard of installed.values()) guard.dispose()
      installed.clear()
    },
  }
}

export const name = PLUGIN_NAME
export const inject = ['tools']

export function createHooks(profile: ProfileFile, gatewayBaseUrl: string) {
  return {
    modelTransport: createModelTransportHook({
      providers: profile.modelGateway.providers,
      gatewayBaseUrl,
    }),
  }
}
