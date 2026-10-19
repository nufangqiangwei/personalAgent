<script lang="ts">
/** One delivered source. `published_at` is absent when it could not be confirmed. */
export interface ArtifactSource {
  source_id?: string
  url: string
  normalized_url?: string
  title?: string
  fetched_at?: string
  published_at?: string | null
  content_hash?: string
}
</script>

<script setup lang="ts">
/**
 * Source list for a delivered document.
 *
 * Only http/https targets become links; anything else is shown as text. A missing
 * publication date is stated as unknown and is never inferred from the fetch time.
 * Fetch time is always shown because it is the one timestamp that is certain.
 */
const props = defineProps<{ sources: ArtifactSource[] }>()

function safeHref(raw: string | undefined): string | null {
  if (!raw) return null
  const trimmed = raw.trim()
  if (trimmed.startsWith('//')) return null
  try {
    const url = new URL(trimmed)
    return url.protocol === 'http:' || url.protocol === 'https:' ? trimmed : null
  } catch {
    return null
  }
}

function label(source: ArtifactSource): string {
  return source.title?.trim() || source.url
}
</script>

<template>
  <div>
    <ul v-if="props.sources.length > 0" class="sources">
      <li v-for="source in props.sources" :key="source.source_id ?? source.url">
        <a
          v-if="safeHref(source.url)"
          :href="safeHref(source.url) ?? undefined"
          target="_blank"
          rel="noopener noreferrer"
        >
          {{ label(source) }}
        </a>
        <span v-else>
          {{ label(source) }}（链接已阻止：只允许 http/https）
        </span>
        <div class="muted meta">
          <span v-if="source.fetched_at">获取时间 {{ source.fetched_at }}</span>
          <span v-if="source.published_at">· 发布时间 {{ source.published_at }}</span>
          <span v-else>· 发布时间未注明（不以获取时间推断）</span>
        </div>
      </li>
    </ul>
    <p v-else class="muted">没有可用来源；相关结论标记为未证实。</p>
  </div>
</template>

<style scoped>
.sources {
  margin: 0;
  padding-left: 1.25rem;
}

.sources li + li {
  margin-top: 0.4rem;
}

.meta {
  font-size: 0.85rem;
}
</style>
