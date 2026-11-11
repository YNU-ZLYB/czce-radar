<script setup lang="ts">
import { computed } from 'vue'

import { directionClass, formatCst } from '../lib/format'
import type { PredictionItem, VarietyMeta } from '../types/api'

const props = defineProps<{
  meta: VarietyMeta | undefined
  prediction: PredictionItem | null
}>()

const emit = defineEmits<{ open: [] }>()

/** 传导一行：取贡献绝对值最大的外盘驱动（value 非空）；无传导贡献时显示季节/无。 */
const transmission = computed(() => {
  const items = props.prediction?.contributor ?? []
  const foreign = items
    .filter((c) => c.value !== null)
    .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
  const top = foreign[0]
  if (top === undefined) return null
  const sign = top.value !== null && top.value > 0 ? '+' : ''
  return `${top.name} ${sign}${top.value?.toFixed(1)}%`
})

const dirClass = computed(() =>
  props.prediction ? directionClass(props.prediction.direction) : 'flat',
)
</script>

<template>
  <button
    class="pcard"
    type="button"
    :aria-label="`${meta?.name ?? prediction?.variety_code ?? '未知品种'} 详情`"
    @click="emit('open')"
  >
    <template v-if="prediction">
      <div class="pcard__top">
        <div class="pcard__name">
          <span class="pcard__cn">{{ meta?.name ?? prediction.variety_code }}</span>
          <span class="pcard__code">{{ prediction.variety_code }}</span>
        </div>
        <span
          class="badge"
          :class="`badge--${dirClass}`"
        >{{ prediction.bucket }}</span>
      </div>
      <div class="pcard__mid">
        <span
          class="pcard__signal"
          :class="`text--${dirClass}`"
        >{{ prediction.signal > 0 ? '+' : '' }}{{ prediction.signal }}</span>
        <span class="pcard__conf">把握 {{ Math.round(prediction.confidence * 100) }}%</span>
      </div>
      <p
        v-if="transmission"
        class="pcard__trans"
      >
        传导：{{ transmission }}
      </p>
      <p
        v-else
        class="pcard__trans pcard__trans--none"
      >
        无显著传导
      </p>
      <p class="pcard__time">
        更新 {{ formatCst(prediction.asof) }}
      </p>
    </template>
    <template v-else>
      <div class="pcard__top">
        <div class="pcard__name">
          <span class="pcard__cn">{{ meta?.name ?? '—' }}</span>
          <span class="pcard__code">{{ meta?.code ?? '' }}</span>
        </div>
        <span class="badge badge--flat">无数据</span>
      </div>
      <p class="pcard__trans pcard__trans--none">
        今日预测缺失
      </p>
    </template>
  </button>
</template>
