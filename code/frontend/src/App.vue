<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <header class="app-header">
    <strong>Personal-Agent</strong>
    <nav aria-label="主导航">
      <RouterLink to="/">工作流</RouterLink>
      <RouterLink to="/runs">执行历史</RouterLink>
      <RouterLink to="/data">数据与维护</RouterLink>
      <RouterLink to="/setup">首次配置</RouterLink>
    </nav>
  </header>
  <main id="main" class="app-main">
    <RouterView />
  </main>
</template>

<style>
:root {
  color-scheme: light dark;
  --pa-bg: #fafafa;
  --pa-fg: #1d1d1f;
  --pa-muted: #5f6368;
  --pa-border: #d7d7db;
  --pa-accent: #1a56db;
  --pa-danger: #b42318;
  --pa-warn: #8a5300;
}

@media (prefers-color-scheme: dark) {
  :root {
    --pa-bg: #101012;
    --pa-fg: #ececf1;
    --pa-muted: #a0a0aa;
    --pa-border: #33333a;
    --pa-accent: #8ab4ff;
    --pa-danger: #ff9b90;
    --pa-warn: #ffd08a;
  }
}

body {
  margin: 0;
  background: var(--pa-bg);
  color: var(--pa-fg);
  font-family: 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif;
  line-height: 1.55;
}

.app-header {
  display: flex;
  gap: 1.5rem;
  align-items: center;
  padding: 0.75rem 1.25rem;
  border-bottom: 1px solid var(--pa-border);
}

.app-header nav {
  display: flex;
  gap: 1rem;
}

.app-header a {
  color: var(--pa-accent);
  text-decoration: none;
}

.app-main {
  max-width: 76rem;
  margin: 0 auto;
  padding: 1.25rem;
}

.skip-link {
  position: absolute;
  left: -999px;
}

.skip-link:focus {
  left: 1rem;
  top: 1rem;
  background: var(--pa-bg);
  padding: 0.5rem;
}

button {
  font: inherit;
  padding: 0.35rem 0.85rem;
  border: 1px solid var(--pa-border);
  border-radius: 0.35rem;
  background: transparent;
  color: inherit;
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

:focus-visible {
  outline: 2px solid var(--pa-accent);
  outline-offset: 2px;
}

.error {
  color: var(--pa-danger);
}

.muted {
  color: var(--pa-muted);
}
</style>
