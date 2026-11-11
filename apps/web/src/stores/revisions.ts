/** 修订推送：SSE 实时 + 初始拉取；供卡片徽标、详情抽屉与简报页共用。 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { fetchRevisions } from '../api/client'
import { RevisionStream, type StreamStatus } from '../api/stream'
import type { RevisionItem } from '../types/api'

const MAX_KEEP = 50

export const useRevisionStore = defineStore('revisions', () => {
  const items = ref<RevisionItem[]>([])
  const alerts = ref<string[]>([])
  const status = ref<StreamStatus>('closed')
  const unread = ref(0)

  let stream: RevisionStream | null = null

  const byVariety = computed(() => {
    const map = new Map<string, RevisionItem[]>()
    for (const item of items.value) {
      const list = map.get(item.variety_code) ?? []
      list.push(item)
      map.set(item.variety_code, list)
    }
    return map
  })

  function push(item: RevisionItem): void {
    if (items.value.some((existing) => existing.id === item.id)) return
    items.value = [...items.value, item].slice(-MAX_KEEP)
    unread.value += 1
  }

  function markRead(): void {
    unread.value = 0
  }

  async function bootstrap(): Promise<void> {
    try {
      const body = await fetchRevisions()
      items.value = body.revisions.slice(-MAX_KEEP)
      alerts.value = body.alerts
    } catch {
      // 初始拉取失败不阻塞页面；SSE 连上后按 since_id 补齐
    }
    if (stream !== null) return
    const lastId = items.value.reduce((max, item) => Math.max(max, item.id), 0)
    stream = new RevisionStream(
      (item) => push(item),
      (s) => {
        status.value = s
      },
      lastId,
    )
    stream.start()
  }

  function stop(): void {
    stream?.stop()
    stream = null
  }

  return { items, alerts, status, unread, byVariety, push, markRead, bootstrap, stop }
})
