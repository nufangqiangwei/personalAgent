<script setup lang="ts">
/**
 * Safe Markdown renderer.
 *
 * Raw HTML is never accepted: the source is escaped first and the renderer only
 * emits a fixed tag whitelist. Links are limited to http/https and always carry
 * rel="noopener noreferrer". Anything else (javascript:, data:, file:) is shown
 * as plain text with a visible warning instead of becoming a clickable link.
 */
import { computed } from 'vue'

const props = defineProps<{ text: string }>()

const ALLOWED_PROTOCOLS = new Set(['http:', 'https:'])

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function safeUrl(raw: string): string | null {
  const trimmed = raw.trim()
  if (trimmed.length === 0) return null
  try {
    const url = new URL(trimmed, 'https://local.invalid')
    if (!ALLOWED_PROTOCOLS.has(url.protocol)) return null
    if (trimmed.startsWith('//')) return null
    return trimmed
  } catch {
    return null
  }
}

function renderInline(source: string): string {
  let text = escapeHtml(source)
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>')
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label: string, target: string) => {
    const url = safeUrl(target)
    if (url === null) {
      return `${label}（链接已阻止：只允许 http/https）`
    }
    return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`
  })
  return text
}

function renderBlocks(markdown: string): string {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n')
  const output: string[] = []
  let paragraph: string[] = []
  let list: { type: 'ul' | 'ol'; items: string[] } | null = null
  let fence: string[] | null = null

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      output.push(`<p>${paragraph.map(renderInline).join('<br />')}</p>`)
      paragraph = []
    }
  }
  const flushList = (): void => {
    if (list !== null) {
      const items = list.items.map((item) => `<li>${renderInline(item)}</li>`).join('')
      output.push(`<${list.type}>${items}</${list.type}>`)
      list = null
    }
  }

  for (const line of lines) {
    if (line.trimStart().startsWith('```')) {
      if (fence === null) {
        flushParagraph()
        flushList()
        fence = []
      } else {
        output.push(`<pre><code>${escapeHtml(fence.join('\n'))}</code></pre>`)
        fence = null
      }
      continue
    }
    if (fence !== null) {
      fence.push(line)
      continue
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      flushParagraph()
      flushList()
      const level = heading[1]?.length ?? 1
      output.push(`<h${level}>${renderInline(heading[2] ?? '')}</h${level}>`)
      continue
    }
    const unordered = /^\s*[-*]\s+(.*)$/.exec(line)
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (unordered || ordered) {
      flushParagraph()
      const type = unordered ? 'ul' : 'ol'
      if (list === null || list.type !== type) {
        flushList()
        list = { type, items: [] }
      }
      list.items.push((unordered?.[1] ?? ordered?.[1]) ?? '')
      continue
    }
    if (line.trim() === '') {
      flushParagraph()
      flushList()
      continue
    }
    flushList()
    paragraph.push(line)
  }
  if (fence !== null) {
    output.push(`<pre><code>${escapeHtml(fence.join('\n'))}</code></pre>`)
  }
  flushParagraph()
  flushList()
  return output.join('\n')
}

const html = computed(() => renderBlocks(props.text))
</script>

<template>
  <div class="safe-markdown" v-html="html" />
</template>

<style scoped>
.safe-markdown :deep(h1),
.safe-markdown :deep(h2),
.safe-markdown :deep(h3) {
  margin: 1rem 0 0.5rem;
}

.safe-markdown :deep(code) {
  padding: 0.1rem 0.3rem;
  border-radius: 0.25rem;
  background: rgb(127 127 127 / 15%);
}

.safe-markdown :deep(pre) {
  overflow-x: auto;
  padding: 0.75rem;
  background: rgb(127 127 127 / 12%);
}

.safe-markdown :deep(a) {
  color: var(--pa-accent);
}
</style>
