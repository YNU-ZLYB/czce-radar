<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'

import { fetchVarieties } from '../api/client'
import PredictionCard from '../components/PredictionCard.vue'
import { formatDay } from '../lib/format'
import { useDashboardStore } from '../stores/dashboard'
import type { PredictionItem, VarietyMeta } from '../types/api'

// 抽屉含 ECharts，按需加载以缩小首屏 chunk
const DetailDrawer = defineAsyncComponent(() => import('../components/DetailDrawer.vue'))

const store = useDashboardStore()

const varieties = ref<VarietyMeta[]>([])
const openCode = ref<string | null>(null)

const byCode = computed(() => {
  const map = new Map<string, PredictionItem>()
  for (const item of store.predictions) map.set(item.variety_code, item)
  return map
})

const metaByCode = computed(() => new Map(varieties.value.map((v) => [v.code, v])))

const orderedCodes = computed(() => {
  if (varieties.value.length > 0) return varieties.value.map((v) => v.code)
  return store.predictions.map((p) => p.variety_code)
})

const openMeta = computed(() =>
  openCode.value === null ? undefined : metaByCode.value.get(openCode.value),
)
const openPrediction = computed(() =>
  openCode.value === null ? null : (byCode.value.get(openCode.value) ?? null),
)

const showSkeleton = computed(
  () => store.loading && store.predictions.length === 0 && varieties.value.length === 0,
)

onMounted(() => {
  store.hydrate()
  void store.refresh()
  void fetchVarieties()
    .then((body) => {
      varieties.value = body.varieties
    })
    .catch(() => {
      // 品种元数据失败时退化为仅代码展示
    })
})
</script>

<template>
  <div>
    <div class="homebar">
      <h2 class="homebar__title">
        今日研判
        <span
          v-if="store.targetDate"
          class="homebar__date"
        >目标日 {{ formatDay(store.targetDate) }}</span>
      </h2>
      <button
        class="homebar__refresh"
        type="button"
        :disabled="store.loading"
        @click="store.refresh()"
      >
        {{ store.loading ? '刷新中' : '刷新' }}
      </button>
    </div>

    <p
      v-if="store.stale && store.predictions.length > 0"
      class="alert"
    >
      数据可能过期：当前展示的是缓存卡片
      <template v-if="store.error">
        （刷新失败：{{ store.error }}）
      </template>
    </p>

    <div
      v-if="showSkeleton"
      class="grid"
    >
      <div
        v-for="i in 9"
        :key="i"
        class="skeleton skeleton--card"
      />
    </div>

    <div
      v-else-if="orderedCodes.length > 0"
      class="grid"
    >
      <PredictionCard
        v-for="code in orderedCodes"
        :key="code"
        :meta="metaByCode.get(code)"
        :prediction="byCode.get(code) ?? null"
        @open="openCode = code"
      />
    </div>

    <p
      v-else
      class="alert"
    >
      暂无预测数据<template v-if="store.error">
        ：{{ store.error }}
      </template>
    </p>

    <DetailDrawer
      v-if="openCode !== null"
      :code="openCode"
      :meta="openMeta"
      :prediction="openPrediction"
      @close="openCode = null"
    />
  </div>
</template>
