<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { fetchVarietyDetail } from '../api/client'
import { directionClass, formatCst, signed } from '../lib/format'
import type { PredictionItem, VarietyDetail, VarietyMeta } from '../types/api'
import TrendChart from './TrendChart.vue'

const props = defineProps<{
  code: string
  meta: VarietyMeta | undefined
  prediction: PredictionItem | null
}>()

const emit = defineEmits<{ close: [] }>()

const detail = ref<VarietyDetail | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)

let abort: AbortController | null = null

async function load(): Promise<void> {
  abort?.abort()
  abort = new AbortController()
  loading.value = true
  loadError.value = null
  try {
    detail.value = await fetchVarietyDetail(props.code, abort.signal)
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === 'AbortError') return
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => props.code,
  () => {
    detail.value = null
    void load()
  },
  { immediate: true },
)

onBeforeUnmount(() => abort?.abort())

const shown = computed(() => detail.value?.prediction ?? props.prediction)
const dirClass = computed(() =>
  shown.value ? directionClass(shown.value.direction) : 'flat',
)

const contributors = computed(() => {
  const items = shown.value?.contributor ?? []
  const max = items.reduce((m, c) => Math.max(m, Math.abs(c.contribution)), 0)
  return items
    .slice()
    .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
    .map((c) => ({
      ...c,
      pct: max > 0 ? Math.abs(c.contribution) / max : 0,
    }))
})

const calibration = computed(() => detail.value?.model?.calibration ?? {})

const CHAIN_STAGE: Record<string, string> = {
  upstream: '上游',
  mid: '中游',
  downstream: '下游',
  demand: '需求',
}

function stageLabel(stage: string | null): string {
  return stage === null ? '' : (CHAIN_STAGE[stage] ?? stage)
}
</script>

<template>
  <Teleport to="body">
    <div
      class="drawer-mask"
      @click.self="emit('close')"
    >
      <section
        class="drawer"
        role="dialog"
        aria-modal="true"
        :aria-label="`${meta?.name ?? code} 详情`"
      >
        <header class="drawer__head">
          <div class="drawer__title">
            <h2>{{ meta?.name ?? code }}</h2>
            <span class="drawer__sub">{{ code }} · {{ meta?.sector ?? '—' }}</span>
          </div>
          <button
            class="drawer__close"
            type="button"
            aria-label="关闭"
            @click="emit('close')"
          >
            ✕
          </button>
        </header>

        <div class="drawer__body">
          <div
            v-if="shown"
            class="drawer__summary"
          >
            <span
              class="badge badge--lg"
              :class="`badge--${dirClass}`"
            >{{ shown.bucket }}</span>
            <div class="drawer__nums">
              <span
                class="drawer__signal"
                :class="`text--${dirClass}`"
              >{{ shown.signal > 0 ? '+' : '' }}{{ shown.signal }}</span>
              <span class="drawer__conf">
                把握 {{ Math.round(shown.confidence * 100) }}%
                · 涨 {{ Math.round(shown.p_up * 100) }}%
                / 平 {{ Math.round(shown.p_flat * 100) }}%
                / 跌 {{ Math.round(shown.p_down * 100) }}%
              </span>
            </div>
            <span class="drawer__date">目标日 {{ shown.target_date.slice(5) }}</span>
          </div>

          <p
            v-if="shown?.narrative"
            class="drawer__narrative"
          >
            {{ shown.narrative }}
          </p>

          <section class="section">
            <h3>近 60 日走势与信号</h3>
            <div
              v-if="loading"
              class="skeleton skeleton--chart"
            />
            <TrendChart
              v-else-if="detail"
              :prices="detail.prices"
              :signals="detail.signal_history"
            />
            <p
              v-else
              class="section__empty"
            >
              {{ loadError ?? '暂无数据' }}
            </p>
          </section>

          <section
            v-if="contributors.length > 0"
            class="section"
          >
            <h3>贡献者</h3>
            <ul class="cbars">
              <li
                v-for="c in contributors"
                :key="c.name"
                class="cbar"
              >
                <span class="cbar__name">{{ c.name }}</span>
                <span class="cbar__track">
                  <span
                    class="cbar__fill"
                    :class="c.contribution >= 0 ? 'cbar__fill--up' : 'cbar__fill--down'"
                    :style="{ width: `${Math.round(c.pct * 100)}%` }"
                  />
                </span>
                <span
                  class="cbar__val"
                  :class="c.contribution >= 0 ? 'text--up' : 'text--down'"
                >{{ signed(c.contribution, 2) }}</span>
              </li>
            </ul>
          </section>

          <section class="section">
            <h3>利多 / 利空（近 7 日）</h3>
            <div
              v-if="loading"
              class="skeleton skeleton--lines"
            />
            <template v-else-if="detail">
              <div
                v-if="detail.bullish.length === 0 && detail.bearish.length === 0"
                class="section__empty"
              >
                近 7 日无相关资讯
              </div>
              <ul
                v-if="detail.bullish.length > 0"
                class="news"
              >
                <li
                  v-for="n in detail.bullish"
                  :key="`b${n.doc_id}`"
                >
                  <a
                    v-if="n.url"
                    :href="n.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="news__link text--up"
                  >▲ {{ n.title }}</a>
                  <span
                    v-else
                    class="text--up"
                  >▲ {{ n.title }}</span>
                  <span class="news__meta">
                    {{ n.source }} · {{ formatCst(n.published_at) }}
                    <template v-if="n.chain_stage"> · {{ stageLabel(n.chain_stage) }}</template>
                  </span>
                </li>
              </ul>
              <ul
                v-if="detail.bearish.length > 0"
                class="news"
              >
                <li
                  v-for="n in detail.bearish"
                  :key="`s${n.doc_id}`"
                >
                  <a
                    v-if="n.url"
                    :href="n.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="news__link text--down"
                  >▼ {{ n.title }}</a>
                  <span
                    v-else
                    class="text--down"
                  >▼ {{ n.title }}</span>
                  <span class="news__meta">
                    {{ n.source }} · {{ formatCst(n.published_at) }}
                    <template v-if="n.chain_stage"> · {{ stageLabel(n.chain_stage) }}</template>
                  </span>
                </li>
              </ul>
            </template>
          </section>

          <section
            v-if="detail && detail.revisions.length > 0"
            class="section"
          >
            <h3>盘中修订</h3>
            <ul class="news">
              <li
                v-for="r in detail.revisions"
                :key="r.id"
              >
                <span>
                  {{ formatCst(r.at) }} · 信号 {{ r.prev_signal ?? '—' }} →
                  <strong>{{ r.new_signal ?? '—' }}</strong>
                  <template v-if="r.is_surprise">（预期差）</template>
                </span>
                <span class="news__meta">{{ r.reason }}</span>
                <a
                  v-if="r.trigger_doc?.url"
                  :href="r.trigger_doc.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="news__link"
                >原文：{{ r.trigger_doc.title }}</a>
              </li>
            </ul>
          </section>

          <section
            v-if="detail?.model"
            class="section section--muted"
          >
            <h3>模型与校准</h3>
            <p class="model">
              版本 {{ detail.model.version }}（{{ detail.model.kind }} · {{ detail.model.status }}）
            </p>
            <p class="model model--small">
              样本外：ECE {{ calibration.ece ?? '—' }} · 方向 Brier
              {{ calibration.brier_direction ?? '—' }} · 方向命中
              {{ calibration.direction_accuracy == null
                ? '—'
                : `${Math.round(Number(calibration.direction_accuracy) * 100)}%` }}
              <template v-if="calibration.window">
                （{{ calibration.window }}）
              </template>
            </p>
            <p class="model model--small">
              置信度为校准后概率：把握 60% 的预测，历史命中率应接近 60%（ECE 越小越准）。
            </p>
          </section>
        </div>
      </section>
    </div>
  </Teleport>
</template>
