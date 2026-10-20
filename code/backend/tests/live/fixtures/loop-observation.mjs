// Fixed in-memory model observation for the REAL official Loop adoption probe.
// It never calls next(): no supplier request or credential access is possible.
export const name = 'personal-agent-offline-loop-observation'
export const inject = ['llm']
export function apply(ctx, config = {}) {
  ctx.on('llm/stream', async function* () {
    if (config.delayMs) await new Promise(resolve => setTimeout(resolve, config.delayMs))
    yield { type: 'block-start', index: 0, blockType: 'reasoning' }
    yield { type: 'reasoning-delta', index: 0, text: 'PRIVATE_ADOPTION_THOUGHT' }
    yield { type: 'block-end', index: 0,
      block: { type: 'reasoning', text: 'PRIVATE_ADOPTION_THOUGHT' } }
    const text = '{"answer":"CLI_OFFLINE_OK"}'
    yield { type: 'block-start', index: 1, blockType: 'text' }
    yield { type: 'text-delta', index: 1, text }
    yield { type: 'block-end', index: 1, block: { type: 'text', text } }
    yield { type: 'usage', usage: { inputTokens: 3, cacheReadTokens: 10,
      outputTokens: 2, totalTokens: 15 } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  })
}
