import { defineStore } from 'pinia'
import { ref } from 'vue'

import { fetchHealth } from '../api/client'
import type { HealthResponse } from '../types/health'

const REQUEST_TIMEOUT_MS = 8000

export const useHealthStore = defineStore('health', () => {
  const data = ref<HealthResponse | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const lastCheckedAt = ref<number | null>(null)

  async function refresh(): Promise<void> {
    if (loading.value) return
    loading.value = true
    error.value = null

    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
    try {
      data.value = await fetchHealth(controller.signal)
      lastCheckedAt.value = Date.now()
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        error.value = `请求超时（${REQUEST_TIMEOUT_MS / 1000}s）`
      } else {
        error.value = e instanceof Error ? e.message : '未知错误'
      }
    } finally {
      window.clearTimeout(timer)
      loading.value = false
    }
  }

  return { data, loading, error, lastCheckedAt, refresh }
})
