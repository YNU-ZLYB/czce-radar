<script setup lang="ts">
import { computed } from 'vue'
import type { EChartsCoreOption } from 'echarts/core'

import { useChart } from '../lib/chart'
import type { PricePoint, SignalPoint } from '../types/api'

const props = defineProps<{
  prices: PricePoint[]
  signals: SignalPoint[]
}>()

const UP = '#d92b2b'
const DOWN = '#0e9f6e'
const FLAT = '#94a3b8'

const option = computed<EChartsCoreOption | null>(() => {
  if (props.prices.length === 0 && props.signals.length === 0) return null
  const dates = Array.from(
    new Set([
      ...props.prices.map((p) => p.date),
      ...props.signals.map((s) => s.target_date),
    ]),
  ).sort()
  const priceByDate = new Map(props.prices.map((p) => [p.date, p.close]))
  const signalByDate = new Map(props.signals.map((s) => [s.target_date, s]))
  return {
    grid: { left: 44, right: 40, top: 24, bottom: 20 },
    tooltip: {
      trigger: 'axis',
      formatter: (params: unknown) => {
        const list = params as Array<{ axisValue: string }>
        const day = list[0]?.axisValue ?? ''
        const price = priceByDate.get(day)
        const sig = signalByDate.get(day)
        const lines = [day]
        if (price !== undefined) lines.push(`收盘 ${price}`)
        if (sig !== undefined) {
          lines.push(`信号 ${sig.signal}（${sig.bucket}）`)
        }
        return lines.join('<br/>')
      },
    },
    xAxis: {
      type: 'category',
      data: dates.map((d) => d.slice(5)),
      axisLabel: { fontSize: 10, color: '#94a3b8' },
      axisLine: { lineStyle: { color: '#e5e9f2' } },
      axisTick: { show: false },
    },
    yAxis: [
      {
        scale: true,
        axisLabel: { fontSize: 10, color: '#94a3b8' },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      {
        min: -100,
        max: 100,
        axisLabel: { fontSize: 10, color: '#94a3b8' },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '收盘',
        type: 'line',
        data: dates.map((d) => priceByDate.get(d) ?? null),
        showSymbol: false,
        lineStyle: { width: 1.5, color: '#1d4ed8' },
        itemStyle: { color: '#1d4ed8' },
        connectNulls: true,
      },
      {
        name: '信号',
        type: 'bar',
        yAxisIndex: 1,
        data: dates.map((d) => {
          const sig = signalByDate.get(d)
          if (sig === undefined) return null
          return {
            value: sig.signal,
            itemStyle: {
              color: sig.signal > 0 ? UP : sig.signal < 0 ? DOWN : FLAT,
              opacity: 0.55,
            },
          }
        }),
        barMaxWidth: 8,
      },
    ],
  }
})

const { el } = useChart(option)
</script>

<template>
  <div
    ref="el"
    class="trend"
  />
</template>

<style scoped>
.trend {
  width: 100%;
  height: 220px;
}
</style>
