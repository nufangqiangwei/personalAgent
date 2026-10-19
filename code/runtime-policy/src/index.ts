import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  PLUGIN_NAME,
  createModelTransportHook,
  installGuard,
  type InstalledGuard,
  type ToolPolicy,
  type ToolServiceLike,
} from './guard.js'

export * from './guard.js'

interface ProfileFile {
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
export function apply(ctx: { tools: ToolServiceLike }, profile: ProfileFile): InstalledGuard {
  const installed = installGuard(ctx.tools, toolPolicyFromProfile(profile))
  return installed
}

export const name = PLUGIN_NAME

export function createHooks(profile: ProfileFile, gatewayBaseUrl: string) {
  return {
    modelTransport: createModelTransportHook({
      providers: profile.modelGateway.providers,
      gatewayBaseUrl,
    }),
  }
}
