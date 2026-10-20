// Real installed Cordis/tool services, fixed local observations, no model/HTTP.
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { readFileSync } from 'node:fs'

const [entry, policyEntry, profileFile] = process.argv.slice(2)
const requireCli = createRequire(entry)
const load = async name => import(pathToFileURL(requireCli.resolve(name)).href)
const { Context } = await load('@deepseek-ai/cordis')
const { default: SystemPrompt } = await load('@deepseek-ai/dsh-system-prompt')
const { ToolRuntime } = await load('@deepseek-ai/dsh-tools')
const { createScope } = await load('@deepseek-ai/dsh-scope')
const { apply } = await import(pathToFileURL(policyEntry).href)
const profile = JSON.parse(readFileSync(profileFile, 'utf8'))
const ctx = new Context()
new SystemPrompt(ctx, {})
new ToolRuntime(ctx, { mode: 'native' })

let allowedBodies = 0
let deniedBodies = 0
const definition = (name, body) => ({
  name, description: 'Fixed local adoption observation',
  parameters: { type: 'object', properties: {}, additionalProperties: false },
  output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
  execute: async () => { body(); return 'fixed' },
})
for (const name of profile.tools.allow) {
  ctx.tools.register(definition(name, () => { allowedBodies++ }))
}
const agent = { id: 'adoption-agent' }
const scope = createScope(ctx, agent)
agent.ctx = scope.ctx

let summary
try {
  apply(ctx, { ...profile, scope: { scopeToken: 'fixture-local-scope', epoch: 1 } })
  // The pinned dsh-agent dispatches { agent, source, signal }, not the agent itself.
  await ctx.serial('agent/created', { agent, source: 'fresh' })
  // Registered after restrict in the exact same agent scope: visible but denied.
  agent.ctx.tools.register(definition('auto_fix', () => { deniedBodies++ }))
  const call = name => agent.ctx.tools.execute({
    callId: `call-${name}`, name, arguments: {}, agent, signal: new AbortController().signal,
  })
  const denied = await call('auto_fix')
  const allowed = await call('search.web')
  summary = {
    tool_scope_denied: denied.isError === true && deniedBodies === 0,
    allowed_tool_executed: allowed.isError === false && allowedBodies === 1,
    same_scope_registered_after_guard: true,
  }
} catch {
  summary = { tool_scope_denied: false, allowed_tool_executed: false }
} finally {
  await scope.dispose()
  await ctx.fiber.dispose()
}
console.log(JSON.stringify(summary))
process.exitCode = summary.tool_scope_denied && summary.allowed_tool_executed ? 0 : 1
