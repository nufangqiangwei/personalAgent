import { describe, expect, it, vi } from 'vitest'

import { createModelTransportHook, createToolGuard, installGuard } from '../src/guard.js'
import { apply, loadProfile, toolPolicyFromProfile } from '../src/index.js'

const policy = {
  allow: ['search.web', 'http.fetch', 'browser.read', 'content.extract'],
  denyPrefixes: ['shell', 'fs', 'agent', 'mcp.admin'],
  denyManagementApi: true,
  emptyDomainListDeniesNetwork: true,
}

const scopeToken = 'scope-token'

describe('restricted tool guard', () => {
  it('uses the pinned Harness guard convention: a string denies, undefined allows', () => {
    let check: ((execution: { name: string }) => string | undefined) | undefined
    const releaseRestriction = vi.fn()
    const releaseGuard = vi.fn()
    const tools = {
      restrict: vi.fn(() => releaseRestriction),
      guard: vi.fn((callback: typeof check) => { check = callback; return releaseGuard }),
    }
    const installed = installGuard(tools, policy, { scopeToken, epoch: 1 })
    expect(check?.({ name: 'shell.exec' })).toEqual(expect.any(String))
    expect(check?.({ name: 'search.web' })).toBeUndefined()
    installed.dispose()
    expect(releaseGuard).toHaveBeenCalledOnce()
    expect(releaseRestriction).toHaveBeenCalledOnce()
  })

  it('fails closed when the official guard or restriction service is absent', () => {
    expect(() => installGuard({ restrict: vi.fn() }, policy)).toThrow()
    expect(() => installGuard({ guard: vi.fn() }, policy)).toThrow()
  })

  it('allows a profile tool with a scope token', () => {
    const guard = createToolGuard(policy)
    expect(guard({ tool: 'search.web', scopeToken, epoch: 1, domains: ['example.com'] })).toEqual({
      allowed: true,
    })
  })

  it('rejects an unlisted tool even when it was registered in the same scope', () => {
    const guard = createToolGuard(policy)
    const decision = guard({ tool: 'auto_fix', scopeToken, epoch: 1 })
    expect(decision.allowed).toBe(false)
    expect(decision.code).toBe('TOOL_NOT_ALLOWED')
  })

  it('deny wins over allow for a newly registered dangerous tool', () => {
    const guard = createToolGuard({ ...policy, allow: [...policy.allow, 'shell.exec'] })
    const decision = guard({ tool: 'shell.exec', scopeToken, epoch: 1 })
    expect(decision.allowed).toBe(false)
    expect(decision.code).toBe('TOOL_NOT_ALLOWED')
  })

  it.each(['fs.read', 'fs/write', 'agent.spawn', 'mcp.admin.list', 'shell'])(
    'rejects forbidden capability %s',
    (tool) => {
      const guard = createToolGuard(policy)
      expect(guard({ tool, scopeToken, epoch: 1 }).allowed).toBe(false)
    },
  )

  it('rejects calls into the management API', () => {
    const guard = createToolGuard(policy)
    const decision = guard({ tool: 'http.fetch', scopeToken, epoch: 1, managementApi: true })
    expect(decision.allowed).toBe(false)
    expect(decision.code).toBe('MANAGEMENT_API_DENIED')
  })

  it('requires a node scope token', () => {
    const guard = createToolGuard(policy)
    expect(guard({ tool: 'http.fetch', epoch: 1 }).code).toBe('SCOPE_TOKEN_MISSING')
    expect(guard({ tool: 'http.fetch', scopeToken: '   ', epoch: 1 }).code).toBe('SCOPE_TOKEN_MISSING')
  })

  it('rejects an invalid epoch so stale attempts cannot act', () => {
    const guard = createToolGuard(policy)
    expect(guard({ tool: 'http.fetch', scopeToken, epoch: 0 }).code).toBe('EPOCH_MISMATCH')
    expect(guard({ tool: 'http.fetch', scopeToken, epoch: 1.5 }).code).toBe('EPOCH_MISMATCH')
  })

  it('denies network access when the domain list is empty', () => {
    const guard = createToolGuard(policy)
    const decision = guard({ tool: 'http.fetch', scopeToken, epoch: 1, domains: [] })
    expect(decision.allowed).toBe(false)
    expect(decision.code).toBe('DOMAIN_LIST_EMPTY')
  })

  it('installs restrict and guard without stacking on dispose', () => {
    const restrict = vi.fn()
    const dispose = vi.fn()
    const guard = vi.fn(() => dispose)
    const tools = { restrict, guard }

    const installed = installGuard(tools, policy)
    expect(restrict).toHaveBeenCalledWith({ allow: policy.allow })
    expect(guard).toHaveBeenCalledOnce()
    expect(installed.decision({ tool: 'http.fetch', scopeToken, epoch: 1 }).allowed).toBe(true)

    installed.dispose()
    expect(dispose).toHaveBeenCalledOnce()
  })

  it('skips restrict when no allowed tool exists yet and still guards', () => {
    // The pinned service rejects unknown global names; an empty roster must not
    // turn the guard into a startup failure. The guard stays authoritative.
    const restrict = vi.fn()
    const guard = vi.fn(() => () => undefined)
    const tools = { restrict, guard, get: () => undefined }

    const installed = installGuard(tools, policy)
    expect(restrict).not.toHaveBeenCalled()
    expect(guard).toHaveBeenCalledOnce()
    expect(installed.decision({ tool: 'http.fetch', scopeToken, epoch: 1 }).allowed).toBe(true)
    expect(installed.decision({ tool: 'auto_fix', scopeToken, epoch: 1 }).allowed).toBe(false)
  })

  it('restricts exactly the registered allowlist names', () => {
    const restrict = vi.fn()
    const guard = vi.fn(() => () => undefined)
    const registered = new Set(['search.web'])
    const tools = { restrict, guard, get: (name: string) => registered.has(name) ? {} : undefined }

    installGuard(tools, policy)
    expect(restrict).toHaveBeenCalledWith({ allow: ['search.web'] })
  })

  it('binds the node scope from the launch environment when the profile has none', () => {
    const predicates: Array<(execution: { name: string }) => string | undefined> = []
    const guard = vi.fn((predicate: (execution: { name: string }) => string | undefined) => {
      predicates.push(predicate)
      return () => undefined
    })
    const restrict = vi.fn()
    const listeners: Array<(payload: { agent: { ctx: { tools: unknown } } }) => void> = []
    const on = (name: string, listener: (payload: { agent: { ctx: { tools: unknown } } }) => void) => {
      if (name === 'agent/created') listeners.push(listener)
      return () => undefined
    }
    process.env.PERSONAL_AGENT_GATEWAY_TOKEN = 'attempt-token'
    process.env.PERSONAL_AGENT_SCOPE_EPOCH = '7'
    try {
      apply({ tools: { guard, restrict }, on } as never, loadProfile())
      listeners[0]({ agent: { ctx: { tools: { guard, restrict } } } })
      // A positive epoch and the attempt token satisfy the node scope check.
      expect(predicates[0]({ name: 'search.web' })).toBeUndefined()
    } finally {
      delete process.env.PERSONAL_AGENT_GATEWAY_TOKEN
      delete process.env.PERSONAL_AGENT_SCOPE_EPOCH
    }
  })

  it('fails closed when neither the profile nor the environment binds a scope', () => {
    const predicates: Array<(execution: { name: string }) => string | undefined> = []
    const guard = vi.fn((predicate: (execution: { name: string }) => string | undefined) => {
      predicates.push(predicate)
      return () => undefined
    })
    const restrict = vi.fn()
    const listeners: Array<(payload: { agent: { ctx: { tools: unknown } } }) => void> = []
    const on = (name: string, listener: (payload: { agent: { ctx: { tools: unknown } } }) => void) => {
      if (name === 'agent/created') listeners.push(listener)
      return () => undefined
    }
    delete process.env.PERSONAL_AGENT_GATEWAY_TOKEN
    apply({ tools: { guard, restrict }, on } as never, loadProfile())
    listeners[0]({ agent: { ctx: { tools: { guard, restrict } } } })
    expect(predicates[0]({ name: 'search.web' })).toBe('SCOPE_TOKEN_MISSING')
  })

  it('loads the shipped profile and mounts only the guard hook', () => {
    const profile = loadProfile()
    const guard = vi.fn(() => () => undefined)
    const restrict = vi.fn()
    const listeners = new Map<string, (payload: { agent: { ctx: { tools: { guard: typeof guard; restrict: typeof restrict } } } }) => void>()
    const on = vi.fn((name, listener) => { listeners.set(name, listener); return () => undefined })
    const installed = apply({ tools: { guard, restrict }, on }, profile)
    expect(restrict).not.toHaveBeenCalled()
    // The pinned dsh-agent dispatches { agent, source, signal }.
    listeners.get('agent/created')?.({ agent: { ctx: { tools: { guard, restrict } } } })
    expect(restrict).toHaveBeenCalledOnce()
    expect(toolPolicyFromProfile(profile)).toEqual({
      allow: profile.tools.allow,
      denyPrefixes: profile.tools.denyPrefixes,
      denyManagementApi: true,
      emptyDomainListDeniesNetwork: true,
    })
    expect(installed.decision({ tool: 'content.extract', scopeToken, epoch: 2 }).allowed).toBe(true)
  })
})

describe('model transport hook', () => {
  const hook = createModelTransportHook({
    providers: ['deepseek-official'],
    gatewayBaseUrl: 'http://127.0.0.1:8787/v1',
  })

  it('allows the configured provider through the local gateway', () => {
    expect(hook({ provider: 'deepseek-official', baseUrl: 'http://127.0.0.1:8787/v1/' })).toEqual({
      allowed: true,
    })
  })

  it('rejects an unconfigured provider or arbitrary proxy target', () => {
    expect(hook({ provider: 'other', baseUrl: 'http://127.0.0.1:8787/v1' }).code).toBe(
      'MODEL_TARGET_NOT_ALLOWED',
    )
    expect(
      hook({ provider: 'deepseek-official', baseUrl: 'https://api.example.com/v1' }).code,
    ).toBe('MODEL_TARGET_NOT_ALLOWED')
  })
})
