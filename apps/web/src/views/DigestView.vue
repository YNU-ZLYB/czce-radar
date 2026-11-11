<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchExtracts } from '../api/client'
import { formatCst } from '../lib/format'
import { useDashboardStore } from '../stores/dashboard'
import { useRevisionStore } from '../stores/revisions'
import type { ExtractItem } from '../types/api'

const dashboard = useDashboardStore()
const revisions = useRevisionStore()

const extracts = ref<ExtractItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

const bucketCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const p of dashboard.predictions) {
    counts[p.bucket] = (counts[p.bucket] ?? 0) + 1
  }
  return ['强多', '偏多', '震荡', '偏空', '强空']
    .filter((b) => counts[b] !== undefined)
    .map((b) => ({ bucket: b, n: counts[b] }))
})

const todayRevisions = computed(() => revisions.items.slice().reverse())

onMounted(() => {
  dashboard.hydrate()
  if (dashboard.predictions.length === 0) void dashboard.refresh()
  fetchExtracts(2)
    .then((body) => {
      extracts.value = body.extracts
    })
    .catch((e: unknown) => {
      error.value = e instanceof Error ? e.message : '加载失败'
    })
    .finally(() => {
      loading.value = false
    })
})
</script>

<template>
  <div class="stack">
    <section class="card">
      <h2 class="card__title">
        今日概览
      </h2>
      <div
        v-if="bucketCounts.length > 0"
        class="chips"
      >
        <span
          v-for="item in bucketCounts"
          :key="item.bucket"
          class="chip"
          :class="{
            'chip--up': item.bucket.includes('多'),
            'chip--down': item.bucket.includes('空'),
          }"
        >{{ item.bucket }} {{ item.n }}</span>
      </div>
      <p
        v-else
        class="section__empty"
      >
        今日预测尚未产出
      </p>
    </section>

    <section class="card">
      <h2 class="card__title">
        盘中修订
        <span
          v-if="revisions.alerts.length > 0"
          class="card__alert"
        >告警：{{ revisions.alerts.join('、') }} 修订频繁</span>
      </h2>
      <ul
        v-if="todayRevisions.length > 0"
        class="news"
      >
        <li
          v-for="r in todayRevisions"
          :key="r.id"
        >
          <span>
            <strong>{{ r.variety_code }}</strong>
            信号 {{ r.prev_signal ?? '—' }} →
            <strong>{{ r.new_signal ?? '—' }}</strong>
            <template v-if="r.is_surprise">（预期差）</template>
          </span>
          <span class="news__meta">{{ formatCst(r.at) }} · {{ r.reason }}</span>
          <a
            v-if="r.trigger_doc?.url"
            :href="r.trigger_doc.url"
            target="_blank"
            rel="noopener noreferrer"
            class="news__link"
          >原文：{{ r.trigger_doc.title }}</a>
        </li>
      </ul>
      <p
        v-else
        class="section__empty"
      >
        今日暂无修订
      </p>
    </section>

    <section class="card">
      <h2 class="card__title">
        相关资讯（近 2 日）
      </h2>
      <div
        v-if="loading"
        class="skeleton skeleton--lines"
      />
      <ul
        v-else-if="extracts.length > 0"
        class="news"
      >
        <li
          v-for="e in extracts"
          :key="e.id"
        >
          <a
            v-if="e.doc?.url"
            :href="e.doc.url"
            target="_blank"
            rel="noopener noreferrer"
            class="news__link"
            :class="e.direction > 0 ? 'text--up' : e.direction < 0 ? 'text--down' : ''"
          >
            {{ e.direction > 0 ? '▲' : e.direction < 0 ? '▼' : '·' }}
            <strong>{{ e.variety_name }}</strong> {{ e.doc.title }}
          </a>
          <span
            v-else
            :class="e.direction > 0 ? 'text--up' : e.direction < 0 ? 'text--down' : ''"
          >
            {{ e.direction > 0 ? '▲' : e.direction < 0 ? '▼' : '·' }}
            <strong>{{ e.variety_name }}</strong> {{ e.doc?.title ?? '' }}
          </span>
          <span class="news__meta">
            {{ e.doc?.source ?? '' }} · {{ formatCst(e.doc?.published_at ?? null) }}
            · 强度 {{ e.strength }}
            <template v-if="e.is_surprise"> · 预期差</template>
          </span>
          <span
            v-if="e.evidence"
            class="news__evidence"
          >依据：{{ e.evidence }}</span>
        </li>
      </ul>
      <p
        v-else
        class="section__empty"
      >
        {{ error ?? '暂无相关资讯（规则抽取保守取向：宁可少抽，不误抽）' }}
      </p>
    </section>
  </div>
</template>
