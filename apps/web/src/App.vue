<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import { useHealthStore } from './stores/health'
import { useRevisionStore } from './stores/revisions'

const health = useHealthStore()
const revisions = useRevisionStore()
const route = useRoute()

const healthClass = computed(() => {
  if (health.error) return 'bad'
  if (!health.data) return 'unknown'
  return health.data.status === 'ok' ? 'ok' : 'warn'
})

const streamText = computed(() => {
  switch (revisions.status) {
    case 'open':
      return '实时'
    case 'connecting':
      return '重连中'
    default:
      return '离线'
  }
})

let timer: number | undefined

watch(
  () => route.name,
  (name) => {
    if (name === 'digest') revisions.markRead()
  },
  { immediate: true },
)

function tick(): void {
  if (document.visibilityState === 'visible') void health.refresh()
}

onMounted(() => {
  void health.refresh()
  void revisions.bootstrap()
  timer = window.setInterval(tick, 30_000)
  document.addEventListener('visibilitychange', tick)
})

onUnmounted(() => {
  revisions.stop()
  if (timer !== undefined) window.clearInterval(timer)
  document.removeEventListener('visibilitychange', tick)
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand__mark">CZ</span>
        <div class="brand__text">
          <h1>CZCE Radar</h1>
          <p>郑商所品种预测研判</p>
        </div>
      </div>
      <div class="topbar__status">
        <span
          class="dot"
          :class="`dot--${healthClass}`"
          :title="health.error ?? health.data?.status ?? '检测中'"
        />
        <span
          class="stream"
          :class="`stream--${revisions.status}`"
        >{{ streamText }}</span>
      </div>
    </header>

    <main class="page">
      <RouterView />
    </main>

    <nav
      class="tabbar"
      aria-label="主导航"
    >
      <RouterLink
        v-for="item in [
          { to: '/', label: '卡片', name: 'home' },
          { to: '/digest', label: '简报', name: 'digest' },
          { to: '/scorecard', label: '评分', name: 'scorecard' },
        ]"
        :key="item.name"
        :to="item.to"
        class="tabbar__item"
        :class="{ 'tabbar__item--active': route.name === item.name }"
      >
        {{ item.label }}
        <i
          v-if="item.name === 'digest' && revisions.unread > 0"
          class="tabbar__badge"
        >{{ revisions.unread > 99 ? '99+' : revisions.unread }}</i>
      </RouterLink>
    </nav>

    <footer class="foot">
      个人研究用途，不构成投资建议
    </footer>
  </div>
</template>
