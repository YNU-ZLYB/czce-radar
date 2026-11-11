/** 首页仪表盘：21 品种卡片；localStorage 缓存上次结果保证冷启动/离线可读。 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { fetchPredictions } from '../api/client'
import type { PredictionItem } from '../types/api'

const CACHE_KEY = 'czce:dashboard:v1'
const STALE_MS = 20 * 3600 * 1000
const REQUEST_TIMEOUT_MS = 8000

interface CachePayload {
  targetDate: string
  fetchedAt: number
  predictions: PredictionItem[]
}

function readCache(): CachePayload | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as CachePayload
    if (!Array.isArray(parsed.predictions)) return null
    return parsed
  } catch {
    return null
  }
}

export const useDashboardStore = defineStore('dashboard', () => {
  const predictions = ref<PredictionItem[]>([])
  const targetDate = ref<string | null>(null)
  const fetchedAt = ref<number | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const fromCache = ref(false)
  /** 本次刷新由 SW 缓存兜底（离线/接口不可达） */
  const servedFromCache = ref(false)

  const stale = computed(() => {
    if (predictions.value.length === 0) return false
    if (servedFromCache.value) return true
    if (error.value !== null) return true
    if (fetchedAt.value === null) return true
    return Date.now() - fetchedAt.value > STALE_MS
  })

  function hydrate(): void {
    const cached = readCache()
    if (cached === null) return
    predictions.value = cached.predictions
    targetDate.value = cached.targetDate
    fetchedAt.value = cached.fetchedAt
    fromCache.value = true
  }

  async function refresh(): Promise<void> {
    if (loading.value) return
    loading.value = true
    error.value = null
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
    try {
      const result = await fetchPredictions(controller.signal)
      const body = result.data
      servedFromCache.value = result.fromCache
      predictions.value = body.predictions
      targetDate.value = body.predictions[0]?.target_date ?? null
      fetchedAt.value = Date.now()
      fromCache.value = false
      if (!result.fromCache) {
        const payload: CachePayload = {
          targetDate: targetDate.value ?? '',
          fetchedAt: fetchedAt.value,
          predictions: body.predictions,
        }
        window.localStorage.setItem(CACHE_KEY, JSON.stringify(payload))
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '未知错误'
    } finally {
      window.clearTimeout(timer)
      loading.value = false
    }
  }

  return {
    predictions,
    targetDate,
    fetchedAt,
    loading,
    error,
    fromCache,
    servedFromCache,
    stale,
    hydrate,
    refresh,
  }
})
