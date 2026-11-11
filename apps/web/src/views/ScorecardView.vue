<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchScorecard } from '../api/client'
import { formatDay, pct, signed } from '../lib/format'
import type { ScorecardResponse } from '../types/api'

const data = ref<ScorecardResponse | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

onMounted(() => {
  fetchScorecard(60)
    .then((body) => {
      data.value = body
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
    <div
      v-if="loading"
      class="skeleton skeleton--card"
    />
    <p
      v-else-if="error"
      class="alert"
    >
      {{ error }}
    </p>

    <template v-else-if="data">
      <section class="card">
        <h2 class="card__title">
          滚动评分（近 {{ data.window_days }} 日 · {{ data.model_version }}）
        </h2>
        <div class="stats">
          <div class="stat">
            <span class="stat__num">{{ pct(data.overall.direction_accuracy) }}</span>
            <span class="stat__label">方向命中（非震荡 {{ data.overall.direction_n }} 条）</span>
          </div>
          <div class="stat">
            <span class="stat__num">{{ pct(data.overall.coverage) }}</span>
            <span class="stat__label">方向覆盖率</span>
          </div>
          <div class="stat">
            <span class="stat__num">{{ data.overall.brier?.toFixed(3) ?? '—' }}</span>
            <span class="stat__label">Brier（三分类）</span>
          </div>
        </div>
        <p class="section__empty section__empty--left">
          口径：方向命中按涨跌符号判定（排除震荡预测与零涨跌日），与回测报告一致。
          评分至 {{ data.scored_through ?? '—' }}。
        </p>
      </section>

      <section
        v-if="data.latest"
        class="card"
      >
        <h2 class="card__title">
          最近交易日 {{ formatDay(data.latest.date) }}
          <span class="card__alert card__alert--plain">
            命中 {{ pct(data.latest.direction_accuracy) }}
          </span>
        </h2>
        <ul class="rows">
          <li
            v-for="item in data.latest.items"
            :key="item.variety_code"
            class="row"
          >
            <div class="row__main">
              <div class="row__text">
                <p class="row__label">
                  {{ item.name }}
                  <span class="row__code">{{ item.variety_code }}</span>
                </p>
                <p class="row__hint">
                  预测 {{ item.bucket }}（{{ item.signal > 0 ? '+' : '' }}{{ item.signal }}）
                  · 实际 {{ item.actual_bucket }} {{ signed(item.actual_change) }}%
                </p>
              </div>
              <span
                class="badge"
                :class="item.hit ? 'badge--up' : 'badge--down'"
              >{{ item.hit ? '命中' : '失误' }}</span>
            </div>
          </li>
        </ul>
      </section>

      <section
        v-if="data.by_variety.length > 0"
        class="card"
      >
        <h2 class="card__title">
          分品种
        </h2>
        <table class="table">
          <thead>
            <tr>
              <th>品种</th>
              <th class="table__num">
                样本
              </th>
              <th class="table__num">
                覆盖率
              </th>
              <th class="table__num">
                方向命中
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="v in data.by_variety"
              :key="v.variety_code"
            >
              <td>{{ v.name }} <span class="row__code">{{ v.variety_code }}</span></td>
              <td class="table__num">
                {{ v.n }}
              </td>
              <td class="table__num">
                {{ pct(v.coverage, 0) }}
              </td>
              <td class="table__num">
                {{ pct(v.direction_accuracy) }}
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </div>
</template>
