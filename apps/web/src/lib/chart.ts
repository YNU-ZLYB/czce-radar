/** ECharts 按需封装：只打包用到的图表与组件，控制体积。 */

import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import type { EChartsCoreOption } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

export function useChart(option: Ref<EChartsCoreOption | null>) {
  const el = ref<HTMLDivElement | null>(null)
  let chart: echarts.ECharts | null = null
  let observer: ResizeObserver | null = null

  const render = () => {
    if (chart === null || option.value === null) return
    chart.setOption(option.value, { notMerge: true })
  }

  onMounted(() => {
    if (el.value === null) return
    chart = echarts.init(el.value)
    render()
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(el.value)
  })

  watch(option, render)

  onBeforeUnmount(() => {
    observer?.disconnect()
    chart?.dispose()
    chart = null
  })

  return { el }
}
